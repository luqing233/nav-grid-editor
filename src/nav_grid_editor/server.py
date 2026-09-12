#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HTTP 服务层：统一路由。

- /map  地图瓦片实时采集/合成 + 标定 + 网格编辑 + 路径标记 + 取坐标
- /     重定向到 /map
- 全部接口的数据层在 map_service.py

由 main.py 创建 MapService 并赋给本模块的 service 后启动服务器；
也可以直接 import 本模块后在测试里驱动 Handler。
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .map_service import MapService

BASE_DIR = Path(__file__).resolve().parent
HOST = "127.0.0.1"

WEB_DIR = BASE_DIR / "web"
COMPOSER_FILE = WEB_DIR / "map_composer.html"

service: MapService | None = None  # main.py 中创建


def _json(data) -> str:
    return json.dumps(data, ensure_ascii=False)


def _image_type(path: Path) -> str:
    """按后缀给 Content-Type（缩略图有 WebP / PNG 两种可能）。"""
    return "image/webp" if path.suffix.lower() == ".webp" else "image/png"


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.1 才能 keep-alive。默认的 HTTP/1.0 每次响应后连接就废了，Chrome
    # 于是每个请求都要新开 socket，而它的 socket 池会先等一会儿看有没有能复用
    # 的连接——表现就是**固定 ~300ms 的停顿**，且只落在那些没能命中浏览器缓存
    # 的请求上（实测同一张缩略图连取三次都是 310ms 左右，而服务端只要 3ms）。
    # 所有响应都带 Content-Length（见 _send_bytes），唯一的流式响应 SSE 在
    # _emit_sse 里显式用 Connection: close 收尾。
    protocol_version = "HTTP/1.1"

    # 关掉 Nagle：响应头与正文是分开的两次 send()，Nagle 会把小的那次压住等
    # ACK。实测顺序取 60 张瓦片 11.5 → 9.0 ms/张——本地回环上没有任何理由
    # 让它开着。
    disable_nagle_algorithm = True

    def log_message(self, fmt, *args):
        sys.stderr.write("[nav-grid-editor] %s\n" % (fmt % args))

    def _path(self) -> str:
        return self.path.split("?", 1)[0]

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            length = 0
        return self.rfile.read(length) if length > 0 else b""

    def _send_bytes(self, data: bytes, content_type: str, status: int = 200,
                    cache: str = "no-store", etag: str | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        if etag:
            self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(data)

    def _not_modified(self, etag: str, cache: str) -> None:
        self.send_response(304)
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _etag_of_bytes(self, data: bytes) -> str:
        """按内容算 ETag（blake2b 8 字节，30KB 瓦片约 0.05ms）。

        不用路径+mtime：瓦片是按坐标寻址的，同坐标的内容会在重抓后变化，
        ETag 必须跟着内容走，否则会拿旧内容回 304。
        """
        return '"' + hashlib.blake2b(data, digest_size=8).hexdigest() + '"'

    def _send_cached_file(self, path: Path, content_type: str, max_age: int) -> bool:
        """带 ETag 发一个文件；命中 If-None-Match 就回 304（不重传正文）。

        返回 True 表示响应已经发出（含 304）。
        """
        try:
            st = path.stat()
            etag = f'"{int(st.st_mtime_ns)}-{st.st_size}"'
            data = path.read_bytes()
        except OSError:
            return False
        cache = f"private, max-age={max_age}, must-revalidate"
        if self.headers.get("If-None-Match") == etag:
            self._not_modified(etag, cache)
            return True
        self._send_bytes(data, content_type, cache=cache, etag=etag)
        return True

    def _send_json(self, obj, status: int = 200):
        self._send_bytes(
            _json(obj).encode("utf-8"),
            "application/json; charset=utf-8",
            status,
        )

    def _read_json(self) -> tuple[dict, str | None]:
        """解析 JSON 请求体，返回 ``(对象, 错误信息)``。

        必须确认结果是 dict 才能用：body 是合法 JSON 的 ``[]`` / ``3`` / ``"x"`` /
        ``null`` 时，直接 ``req.get(...)`` 会抛 AttributeError，连接被断开且没有任何
        JSON 响应，前端只看到请求失败、日志里多一段堆栈。
        """
        try:
            req = json.loads(self._read_body().decode("utf-8") or "{}")
        except Exception as e:
            return {}, f"请求体解析失败: {e}"
        if not isinstance(req, dict):
            return {}, "请求体必须是 JSON 对象"
        return req, None

    # ---------------- GET ----------------
    def do_GET(self):
        path = self._path()
        svc = service

        if path in ("/", "/index.html"):
            # 3D 编辑器已移除，根路径重定向到地图采集/编辑页
            self.send_response(302)
            self.send_header("Location", "/map")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path == "/map":
            if not COMPOSER_FILE.exists():
                self._send_bytes(b"map_composer.html not found", "text/plain; charset=utf-8", 404)
                return
            self._send_bytes(COMPOSER_FILE.read_bytes(), "text/html; charset=utf-8")
            return

        if path == "/api/tilemaps":
            self._send_json({"maps": svc.store.list_maps()})
            return

        if path == "/api/maps":
            qs = parse_qs(urlparse(self.path).query)
            m = (qs.get("map") or [""])[0]
            z = (qs.get("zoom") or [""])[0]
            info = svc.saved_map_info(m, z)
            if info:
                self._send_json({"ok": True, **info})
            else:
                self._send_json({"ok": True, "file": None})
            return

        if path == "/api/overview":
            qs = parse_qs(urlparse(self.path).query)
            m = (qs.get("map") or [""])[0]
            z = (qs.get("zoom") or [""])[0]
            p = svc.ensure_overview(m, z)
            if not p:
                self._send_bytes(b"overview not found", "text/plain; charset=utf-8", 404)
                return
            # max-age=0 + ETag：合成会重新生成缩略图，所以不能让浏览器缓存太久
            # （否则重合成之后还看旧图）。但它只是一个请求，走 304 重验极便宜。
            # 缩略图是 WebP（PNG 的 1/12 大），Content-Type 按后缀给。
            if not self._send_cached_file(p, _image_type(p), max_age=0):
                self._send_bytes(b"overview not found", "text/plain; charset=utf-8", 404)
            return

        if path == "/api/calib":
            qs = parse_qs(urlparse(self.path).query)
            m = (qs.get("map") or [""])[0]
            z = (qs.get("zoom") or [""])[0]
            data = svc.get_calib(m, z)
            if data:
                self._send_json({"ok": True, **data})
            else:
                self._send_json({"ok": True, "file": None})
            return

        if path == "/api/grid2d":
            qs = parse_qs(urlparse(self.path).query)
            m = (qs.get("map") or [""])[0]
            z = (qs.get("zoom") or [""])[0]
            self._send_json({"ok": True, **svc.get_grid2d(m, z)})
            return

        if path == "/api/fetch/status":
            self._send_json(svc.status()["fetch"])
            return

        if path == "/api/marks/status":
            self._send_json(svc.marks_status())
            return

        if path == "/api/events":
            svc.emit_sse(self)
            return

        if path.startswith("/tiles/"):
            parts = path[len("/tiles/"):].split("/")
            m = None
            if len(parts) == 3:
                # 限长 7 位：坐标本来就被 serve_tile 限制在 ±1_000_000，而不限长
                # 的话一个几千位的数字串会让 int() 抛 ValueError（Python 3.12
                # 对 int(str) 有 4300 位上限），把 GET 处理器打断成堆栈
                m = re.fullmatch(r"(-?\d{1,7})_(-?\d{1,7})\.png", parts[2])
            if not m:
                self._send_bytes(b"Not Found", "text/plain; charset=utf-8", 404)
                return
            data = svc.serve_tile(parts[0], parts[1],
                                  int(m.group(1)), int(m.group(2)))
            if data is None:
                self._send_bytes(b"tile not found", "text/plain; charset=utf-8", 404)
                return
            # 瓦片按坐标寻址、内容只在重抓后才变。以前 max-age=30 且没有 ETag，
            # 等于浏览过程中每 30 秒把视野里那几百张瓦片全部重下一遍；现在 5 分钟
            # 有效期 + 内容 ETag：有效期内一个请求都不发，过期后也只回 304。
            cache = "private, max-age=300, must-revalidate"
            etag = self._etag_of_bytes(data)
            if self.headers.get("If-None-Match") == etag:
                self._not_modified(etag, cache)
                return
            self._send_bytes(data, "image/png", cache=cache, etag=etag)
            return

        if path.startswith("/maps/"):
            rest = path[len("/maps/"):]
            cands: list[Path] = []
            # 新结构: maps/<地图>/<zoom>/<文件>.png（只保留一份）
            nested = re.fullmatch(r"([A-Za-z0-9_\-]+)/(\d+)/([A-Za-z0-9_\-]+\.png)", rest)
            if nested:
                cands.append(svc.store.tiles_root / "maps"
                             / nested.group(1) / nested.group(2) / nested.group(3))
            # 旧结构: maps/<文件>.png（兼容早期拼接输出）
            flat = re.fullmatch(r"([A-Za-z0-9_\-]+\.png)", rest)
            if flat:
                cands.append(svc.store.tiles_root / "maps" / flat.group(1))
            for p in cands:
                if p.is_file():
                    self._send_cached_file(p, "image/png", max_age=300)
                    return
            self._send_bytes(b"map not found", "text/plain; charset=utf-8", 404)
            return

        self._send_bytes(b"Not Found", "text/plain; charset=utf-8", 404)

    # ---------------- POST ----------------
    def do_POST(self):
        """所有 POST 的统一异常边界。

        前端一律 `await resp.json()`；服务层任何漏出来的异常（畸形请求触发的
        ValueError/OverflowError/TypeError 等）如果直接冒到 socketserver，只会
        断开连接、前端拿到一个没有原因的失败，日志里只有一段堆栈。这里统一
        翻成一条 JSON 错误。各分支都是"发一次 JSON 就 return"，异常发生时还没
        有任何字节写出去，所以补发响应是安全的。
        """
        try:
            self._do_post()
        except Exception as e:
            sys.stderr.write(f"[nav-grid-editor] POST {self._path()} 异常: {e!r}\n")
            try:
                self._send_json({"ok": False, "error": f"服务端异常: {e}"}, 500)
            except Exception:
                pass

    def _do_post(self):
        path = self._path()
        svc = service

        if path == "/api/fetch/start":
            req, err = self._read_json()
            if err:
                self._send_json({"ok": False, "error": err}, 400)
                return
            self._send_json(svc.start_fetch(headless=bool(req.get("headless", False))))
            return

        if path == "/api/fetch/stop":
            self._send_json(svc.stop_fetch())
            return

        if path == "/api/compose":
            req, err = self._read_json()
            if err:
                self._send_json({"ok": False, "error": err}, 400)
                return
            self._send_json(svc.start_compose(
                str(req.get("map", "")), str(req.get("zoom", "")),
                save=bool(req.get("save", False)),
            ))
            return

        if path == "/api/marks/fetch":
            req, err = self._read_json()
            if err:
                self._send_json({"ok": False, "error": err}, 400)
                return
            self._send_json(svc.start_marks(str(req.get("kind", "public"))))
            return

        if path == "/api/simulate/start":
            self._send_json(svc.start_simulate())
            return

        if path == "/api/simulate/stop":
            self._send_json(svc.stop_simulate())
            return

        if path == "/api/coords":
            self._send_json(svc.relay_coords(self._read_body()))
            return

        if path == "/api/calib":
            req, err = self._read_json()
            if err:
                self._send_json({"ok": False, "error": err}, 400)
                return
            if "threshold" in req:
                try:
                    req["threshold"] = float(req["threshold"])
                except (TypeError, ValueError):
                    req["threshold"] = 5.0
            self._send_json(svc.save_calib(
                str(req.get("map", "")), str(req.get("zoom", "")),
                req.get("points", []), req.get("image_size", {}),
                threshold=req.get("threshold", 5.0),
            ))
            return

        if path == "/api/grid2d":
            req, err = self._read_json()
            if err:
                self._send_json({"ok": False, "error": err}, 400)
                return
            self._send_json(svc.save_grid2d(
                str(req.get("map", "")), str(req.get("zoom", "")),
                req.get("data", {}),
            ))
            return

        self._send_json({"error": "未找到接口"}, 404)


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    """客户端主动断开（WinError 10053/10054/BrokenPipe）时不打印异常堆栈。

    Windows 上浏览器关页面/刷新/中止预连接时，在读下一个请求时可能抛
    ConnectionAbortedError，属于正常噪音，不影响功能，这里静默掉。
    """
    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)
