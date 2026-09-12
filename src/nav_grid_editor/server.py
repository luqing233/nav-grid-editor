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


class Handler(BaseHTTPRequestHandler):
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
                    cache: str = "no-store"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, obj, status: int = 200):
        self._send_bytes(
            _json(obj).encode("utf-8"),
            "application/json; charset=utf-8",
            status,
        )

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
            self._send_bytes(p.read_bytes(), "image/png", cache="private, max-age=30")
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

        if path == "/api/events":
            svc.emit_sse(self)
            return

        if path.startswith("/tiles/"):
            parts = path[len("/tiles/"):].split("/")
            m = None
            if len(parts) == 3:
                m = re.fullmatch(r"(-?\d+)_(-?\d+)\.png", parts[2])
            if not m:
                self._send_bytes(b"Not Found", "text/plain; charset=utf-8", 404)
                return
            data = svc.serve_tile(parts[0], parts[1],
                                  int(m.group(1)), int(m.group(2)))
            if data is None:
                self._send_bytes(b"tile not found", "text/plain; charset=utf-8", 404)
                return
            # 瓦片按坐标寻址、内容很少变：允许浏览器短时缓存，翻回已看过的区域
            # 就不用再打一次本地服务（原来 no-store，每次平移回来都重新请求）。
            self._send_bytes(data, "image/png", cache="private, max-age=30")
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
                    self._send_bytes(p.read_bytes(), "image/png", cache="private, max-age=30")
                    return
            self._send_bytes(b"map not found", "text/plain; charset=utf-8", 404)
            return

        self._send_bytes(b"Not Found", "text/plain; charset=utf-8", 404)

    # ---------------- POST ----------------
    def do_POST(self):
        path = self._path()
        svc = service

        if path == "/api/fetch/start":
            try:
                req = json.loads(self._read_body().decode("utf-8") or "{}")
            except Exception:
                req = {}
            self._send_json(svc.start_fetch(headless=bool(req.get("headless", False))))
            return

        if path == "/api/fetch/stop":
            self._send_json(svc.stop_fetch())
            return

        if path == "/api/compose":
            try:
                req = json.loads(self._read_body().decode("utf-8") or "{}")
            except Exception as e:
                self._send_json({"ok": False, "error": f"请求体解析失败: {e}"}, 400)
                return
            self._send_json(svc.start_compose(
                str(req.get("map", "")), str(req.get("zoom", "")),
                save=bool(req.get("save", False)),
            ))
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
            try:
                req = json.loads(self._read_body().decode("utf-8") or "{}")
            except Exception as e:
                self._send_json({"ok": False, "error": f"请求体解析失败: {e}"}, 400)
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
            try:
                req = json.loads(self._read_body().decode("utf-8") or "{}")
            except Exception as e:
                self._send_json({"ok": False, "error": f"请求体解析失败: {e}"}, 400)
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
