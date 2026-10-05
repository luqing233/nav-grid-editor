# -*- coding: utf-8 -*-
"""地图瓦片、合成、标定和 2D 网格的业务服务。"""

from __future__ import annotations

import json
import math
import os
import queue
import threading
import time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import numpy as np

from .. import marks as marks_mod
from .calibration import _fit_affine_ransac, _invert_affine, _mat_vec
from .events import EventBus
from .grid import _load_grid_npz
from .io import (
    OVERVIEW_SUFFIX,
    _as_int,
    _log,
    _path_lock,
    _replace_with_retry,
    _save_overview,
    _unique_tmp,
    solid_png,
)
from .settings import (
    CELL_BLOCKED,
    CELL_FREE,
    CELL_UNKNOWN,
    DEBUG,
    DENSE_CELL_LIMIT,
    EDIT_CELL_LIMIT,
    GRID_AXIS_CONVENTION,
    GRID_MAGIC,
    GRID_SCHEMA_VERSION,
    GRID_SUFFIX,
    MAP_URL,
    PLAYWRIGHT_OK,
    RUN_PREFIX,
    SIM_COLS,
    SIM_DELAY,
    SIM_MAP,
    SIM_ROWS,
    SIM_ZOOM,
    TILE_SIZE,
    Image,
    default_assets_dir,
    default_data_root,
    default_profile_dir,
    default_tiles_root,
    tile_pattern,
    valid_map_zoom,
)
from .tiles import TileStore, merge_tiles


class FetchState:
    def __init__(self, session_dir: Path, previous: Path | None):
        self.session_dir = Path(session_dir)
        self.previous = previous
        self.downloaded: set[str] = set()
        self.known: set[str] = set()  # 已下载过的瓦片 key（map/zoom/x_y），跨会话去重
        self.counts = {"new": 0, "updated": 0, "unchanged": 0, "error": 0, "skipped": 0}
        #: errors 用 {key: text} 而不是列表：同一张瓦片可能先写失败、重试后成功，
        #: 靠 key（瓦片 rel 或 URL）把旧的那条顶掉，manifest 里才不会同一张瓦片
        #: 既算"失败"又算"新增"
        self.manifest = {"new": [], "updated": [], "errors": {}}
        self.started = datetime.now()

    def add_error(self, key: str, text: str):
        self.manifest["errors"][key] = text

    def clear_error(self, key: str):
        self.manifest["errors"].pop(key, None)

    def save_manifest(self):
        # 有错误也要写：全盘失败（磁盘满/只读卷）时 new/updated 都是 0，
        # 以前会直接 return，于是刚记下来的"哪张瓦片为什么失败"全丢掉——
        # 恰恰是最需要这份记录的场合
        if not (self.counts["new"] or self.counts["updated"]
                or self.manifest["errors"]):
            return
        summary = {
            "session": self.session_dir.name,
            "map_url": MAP_URL,
            "started": self.started.strftime("%Y-%m-%d %H:%M:%S"),
            "ended": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "previous_session": self.previous.name if self.previous else None,
            "counts": self.counts,
            "new": sorted(self.manifest["new"]),
            "updated": sorted(self.manifest["updated"]),
            "errors": list(self.manifest["errors"].values()),
        }
        # 写清单本身失败（卷只读/磁盘满）不能把 _finalize_fetch 带崩：
        # 那样后面的合并到 latest 就整段跳过了
        try:
            self.session_dir.mkdir(parents=True, exist_ok=True)
            (self.session_dir / "manifest.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as e:
            _log(f"抓取清单写入失败 {self.session_dir}: {e}")


# =========================================================
# 地图服务（供 HTTP 层调用）
# =========================================================

class MapService:
    def __init__(self, tiles_root: Path | None = None,
                 profile_dir: Path | None = None,
                 grid2d_dir: Path | None = None,
                 data_root: Path | None = None):
        #: 数据根目录：browser_profile/ configs/ 以及 assets/ 的父目录
        self.data_root = Path(data_root) if data_root else default_data_root()
        self.bus = EventBus()
        self.store = TileStore(tiles_root or default_tiles_root(self.data_root))
        self.profile_dir = profile_dir or default_profile_dir(self.data_root)
        # 2D 网格（无高度）输出目录：可自定义（CLI --grid2d-dir / 环境变量 NAV_GRID2D_DIR）
        env_dir = os.environ.get("NAV_GRID2D_DIR", "")
        self.grid2d_dir = (grid2d_dir
                           or (Path(env_dir) if env_dir else None)
                           or default_assets_dir(self.data_root) / "nav-grid-data")
        self._lock = threading.Lock()
        self._fetch: FetchState | None = None
        self._fetch_thread: threading.Thread | None = None
        self._fetch_stop = threading.Event()
        self._fetch_headless = False
        self._compose: dict | None = None  # {"map","zoom","total","done"}
        self._simulate: dict | None = None
        self._simulation_cancel = threading.Event()
        self._sim_tiles: dict[tuple[int, int], bytes] = {}  # 模拟瓦片（内存）
        # 地图标记抓取（认证口径）：{"running","stats","error"}
        self._marks: dict | None = None
        self._marks_thread: threading.Thread | None = None

    # ---------- 状态快照 ----------
    def status(self) -> dict:
        f = self._fetch
        return {
            "tiles_root": str(self.store.tiles_root),
            "profile_dir": str(self.profile_dir),
            "map_url": MAP_URL,
            "playwright": PLAYWRIGHT_OK,
            "pillow": Image is not None,
            "fetch": {
                "running": self._fetch_thread is not None and self._fetch_thread.is_alive(),
                "headless": self._fetch_headless,
                "session": f.session_dir.name if f else None,
                "counts": dict(f.counts) if f else None,
            },
            "compose": dict(self._compose) if self._compose else None,
            "simulate": dict(self._simulate) if self._simulate else None,
            "marks": dict(self._marks) if self._marks else None,
        }

    # ---------- 事件 ----------
    def emit_sse(self, handler) -> None:
        """SSE 长连接：持续把事件推给浏览器，直到断开"""
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-store")
        # SSE 是无限流，没有 Content-Length。HTTP/1.1 的 keep-alive 下这属于
        # 非法响应（连接无法界定消息边界），所以显式声明 close：正文以连接关闭
        # 结束。EventSource 自己会重连，功能不受影响。
        handler.send_header("Connection", "close")
        handler.send_header("X-Accel-Buffering", "no")
        handler.end_headers()
        q = self.bus.subscribe()
        try:
            handler.wfile.write(("data: " + json.dumps(
                {"type": "hello", "service": self.status()}, ensure_ascii=False
            ) + "\n\n").encode("utf-8"))
            handler.wfile.flush()
            while True:
                try:
                    text = q.get(timeout=1.0)
                    handler.wfile.write(b"data: " + text.encode("utf-8") + b"\n\n")
                    handler.wfile.flush()
                except queue.Empty:
                    continue
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
            pass
        finally:
            self.bus.unsubscribe(q)

    # ---------- 瓦片字节 ----------
    def serve_tile(self, map_name: str, zoom: str, x: int, y: int) -> bytes | None:
        if not valid_map_zoom(map_name, zoom):
            return None
        if not (-1_000_000 <= x <= 1_000_000) \
                or not (-1_000_000 <= y <= 1_000_000):
            return None
        if map_name == SIM_MAP:  # 模拟瓦片直接走内存
            return self._sim_tiles.get((x, y))
        return self.store.tile_bytes(map_name, zoom, x, y)

    # =========================================================
    # 瓦片抓取（Playwright）
    # =========================================================

    def start_fetch(self, headless: bool = False) -> dict:
        if not PLAYWRIGHT_OK:
            return {"ok": False,
                    "error": "未安装 playwright。请先执行: pip install playwright && playwright install chromium"}
        with self._lock:
            if self._fetch_thread and self._fetch_thread.is_alive():
                return {"ok": False, "error": "抓取已在运行中"}
            if self._simulate and self._simulate.get("running"):
                return {"ok": False, "error": "模拟抓取运行中，请先停止模拟"}

            sessions = sorted(
                p for p in self.store.tiles_root.iterdir()
                if p.is_dir() and p.name.startswith(f"{RUN_PREFIX}_")
            ) if self.store.tiles_root.is_dir() else []
            previous = sessions[-1] if sessions else None
            session_name = f"{RUN_PREFIX}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            state = FetchState(self.store.tiles_root / session_name, previous)

            self._fetch_stop.clear()
            self._fetch_headless = bool(headless)
            self._fetch = state
            self.store.active_session_dir = state.session_dir
            self._fetch_thread = threading.Thread(
                target=self._fetch_run, args=(state, bool(headless)),
                name="tile-fetch", daemon=True,
            )
            self._fetch_thread.start()
        self.bus.emit(type="fetch_start", session=state.session_dir.name,
                      headless=bool(headless), previous=previous.name if previous else None)
        self.bus.emit(type="log", text="开始抓取（浏览器"
                      + ("无头模式" if headless else "可见窗口，如需登录请在弹出的浏览器中完成")
                      + "），瓦片将实时贴到地图上")
        return {"ok": True, "session": state.session_dir.name}

    def stop_fetch(self) -> dict:
        with self._lock:
            if not (self._fetch_thread and self._fetch_thread.is_alive()):
                return {"ok": False, "error": "抓取未在运行"}
            self._fetch_stop.set()
        self.bus.emit(type="log", text="正在停止抓取并保存清单…")
        return {"ok": True}

    # ---- 抓取线程 ----
    def _fetch_run(self, state: FetchState, headless: bool):
        try:
            from playwright.sync_api import sync_playwright

            # 已下载过的瓦片集合（latest/会话/旧目录里已有 = 本次不再抓取）
            known = state.known
            for map_name, zooms in self.store.list_maps().items():
                for zoom in zooms:
                    for x, y, _ in self.store.iter_tiles(map_name, zoom):
                        known.add(f"{map_name}/{zoom}/{x}_{y}")
            _log(f"已下载瓦片 {len(known)} 张，本次只抓取未下载过的")
            self.bus.emit(type="log",
                          text=f"已下载瓦片 {len(known)} 张，本次只抓取未下载过的")

            def handle_response(response):
                url = response.url
                try:
                    is_tile = "tile" in url or url.split("?", 1)[0].lower().endswith(".png")
                    if not is_tile or url in state.downloaded:
                        return
                    status = response.status
                    content_type = response.headers.get("content-type", "") or ""
                    if DEBUG:
                        _log(f"[DBG] {status} | {content_type!r} | {url}")
                    if not (200 <= status < 300):
                        state.counts["skipped"] += 1
                        return
                    try:
                        data = response.body()
                    except Exception as e:
                        state.counts["error"] += 1
                        state.add_error(url, f"{url}: {e}")
                        return
                    self._save_tile(state, url, content_type, data)
                except Exception as e:
                    state.counts["error"] += 1
                    state.add_error(url, f"{url}: {e}")
                    _log(f"[错误] {e}")

            def make_route_handler(context):
                def handle_route(route):
                    url = route.request.url
                    try:
                        m = tile_pattern.search(url)
                        if not m:
                            route.continue_()
                            return
                        map_name, zoom, xs, ys = m.groups()
                        if not valid_map_zoom(map_name, zoom):
                            # map_name 会被拼进文件系统路径（tile_bytes / session_dir），
                            # 非白名单值一律不碰本地缓存也不落盘，交回网络
                            route.continue_()
                            return
                        key = f"{map_name}/{zoom}/{xs}/{ys}"
                        if key in known:
                            # 已下载过的瓦片：用本地缓存直接应答，不再请求网络
                            cached = self.store.tile_bytes(map_name, zoom, int(xs), int(ys))
                            if cached is not None:
                                route.fulfill(status=200, content_type="image/png",
                                              body=cached)
                                return
                            known.discard(key)  # 缓存读取失败则退回正常抓取
                        resp = context.request.get(url, headers={"Referer": MAP_URL})
                        if not resp.ok:
                            _log(f"[route] 拉取失败 {resp.status} {url}")
                            route.continue_()
                            return
                        content_type = resp.headers.get("content-type", "") or ""
                        body = resp.body()
                        self._save_tile(state, url, content_type, body)
                        route.fulfill(status=resp.status, content_type=content_type or "image/png",
                                      body=body)
                    except Exception as e:
                        state.counts["error"] += 1
                        # 与 handle_response 一致：错误要落到 manifest，否则只看到
                        # 一个总数，用户无从知道是哪张瓦片、为什么失败
                        state.add_error(url, f"{url}: {e}")
                        _log(f"[route错误] {url} | {e}")
                        try:
                            route.continue_()
                        except Exception:
                            pass
                return handle_route

            self.bus.emit(type="log", text=f"启动浏览器（登录配置: {self.profile_dir}）")
            with sync_playwright() as p:
                launch_kwargs = {
                    "user_data_dir": str(self.profile_dir),
                    "headless": headless,
                }
                if not headless:
                    # 窗口自适应：不固定 viewport（跟随窗口大小，缩放/拖动窗口实时自适应），启动即最大化
                    launch_kwargs["no_viewport"] = True
                    launch_kwargs["args"] = ["--start-maximized"]
                context = p.chromium.launch_persistent_context(**launch_kwargs)
                page = context.new_page()
                page.on("response", handle_response)
                page.route(
                    lambda url: tile_pattern.search(url) is not None,
                    make_route_handler(context),
                )
                page.goto(MAP_URL, timeout=60000)
                self.bus.emit(type="log", text=f"页面已打开: {MAP_URL}，开始监听瓦片请求…")
                while not self._fetch_stop.is_set():
                    page.wait_for_timeout(500)
                context.close()
        except Exception as e:
            state.counts["error"] += 1
            state.add_error("会话", str(e))
            _log(f"抓取异常: {e}")
            self.bus.emit(type="log", text=f"抓取异常: {e}")
        finally:
            self._finalize_fetch(state)

    def _save_tile(self, state: FetchState, url: str, content_type: str,
                   data: bytes):
        if url in state.downloaded:
            return
        state.downloaded.add(url)

        is_png = "image/png" in content_type
        is_webp = "image/webp" in content_type
        if not content_type and url.split("?", 1)[0].lower().endswith(".png"):
            is_png = True
        if not (is_png or is_webp):
            state.counts["skipped"] += 1
            return

        m = tile_pattern.search(url)
        if not m:
            state.counts["skipped"] += 1
            return
        map_name, zoom, xs, ys = m.groups()
        if not valid_map_zoom(map_name, zoom):
            # map_name 会拼进文件系统路径，非白名单值不落盘（正则已收紧，这里兜底）
            state.counts["skipped"] += 1
            return
        try:
            x, y = int(xs), int(ys)
        except ValueError:  # 超长数字串（Python 3.12 对 int(str) 有 4300 位上限）
            state.counts["skipped"] += 1
            return
        if not (-1_000_000 <= x <= 1_000_000 and -1_000_000 <= y <= 1_000_000):
            state.counts["skipped"] += 1
            return
        key = f"{map_name}/{zoom}/{x}_{y}"

        # 已经下载过的瓦片：不再重复下载/写盘，只统计并推送（前端已有则自动跳过）
        if key in state.known:
            state.counts["unchanged"] += 1
            self.bus.emit(type="tile", map=map_name, zoom=zoom, x=x, y=y,
                          kind="unchanged", seq=sum(state.counts.values()))
            return

        rel = f"{map_name}/{zoom}/{x}_{y}.png"

        # WebP → PNG
        if is_webp:
            if Image is None:
                # 不 discard：没装 Pillow 是确定性失败，重试只会把错误数刷上去
                state.counts["error"] += 1
                state.add_error(rel, f"缺少 Pillow，无法转换 WebP: {rel}")
                return
            try:
                img = Image.open(BytesIO(data))
                img = img.convert("RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB")
                buf = BytesIO()
                img.save(buf, "PNG")
                data = buf.getvalue()
            except Exception as e:
                # discard：拿到的是坏 WebP，重新取一次可能就好了；不 discard 的话
                # 这张瓦片本次会话内再也不会被处理
                state.downloaded.discard(url)
                state.counts["error"] += 1
                state.add_error(rel, f"WebP 转换失败 {rel}: {e}")
                return

        # 与上一次会话对比分类：先只算 kind，写盘成功后才记进 manifest，
        # 否则写盘失败时 manifest 会记着一条其实没落盘的瓦片
        try:
            if state.previous is not None:
                prev_file = state.previous / rel
                if prev_file.exists():
                    kind = "unchanged" if prev_file.read_bytes() == data else "updated"
                else:
                    kind = "new"
            else:
                kind = "new"
        except OSError as e:
            state.downloaded.discard(url)
            state.counts["error"] += 1
            state.add_error(rel, f"读取上次会话瓦片失败 {rel}: {e}")
            return

        dest = state.session_dir / rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            # 会话目录是 _compose_candidates 的**第一**候选，也就是读者最先看到
            # 的那份（合成和 /tiles/ 取图都先查它）。直接 write_bytes 会让并发的
            # 读者拿到写了一半的 PNG——解码失败后被当成"这张瓦片缺失"，总图上
            # 从此留一个黑洞。落盘同样走"临时文件 + 原子替换"。
            tmp = _unique_tmp(dest)
            try:
                tmp.write_bytes(data)
                _replace_with_retry(tmp, dest)
            except Exception:
                tmp.unlink(missing_ok=True)
                raise
        except OSError as e:
            # 写盘失败（磁盘满 / 文件被占用）不能算"已下载"：留着标记的话这张
            # 瓦片本次会话内再也不会重试，合成出来是个洞，且 manifest 里查不到
            state.downloaded.discard(url)
            state.counts["error"] += 1
            state.add_error(rel, f"写入失败 {rel}: {e}")
            _log(f"[错误] 写入瓦片失败 {dest}: {e}")
            return

        if kind == "updated":
            state.manifest["updated"].append(rel)
        elif kind == "new":
            state.manifest["new"].append(rel)
        # 重试成功的瓦片要把先前那条失败记录顶掉，否则 manifest 里同一张
        # 瓦片既在 errors 里又在 new/updated 里
        state.clear_error(rel)
        state.counts[kind] += 1
        state.known.add(key)  # 本次已抓到，后续重复请求不再处理

        self.bus.emit(type="tile", map=map_name, zoom=zoom, x=x, y=y,
                      kind=kind, seq=sum(state.counts.values()))

    def _finalize_fetch(self, state: FetchState):
        self.store.active_session_dir = None
        state.save_manifest()
        total = state.counts["new"] + state.counts["updated"] + state.counts["unchanged"]
        self.bus.emit(type="fetch_done",
                      session=state.session_dir.name,
                      counts=dict(state.counts), total=total)
        c = state.counts
        self.bus.emit(type="log",
                      text=f"抓取结束: 新增 {c['new']} 更新 {c['updated']} "
                           f"未变化 {c['unchanged']} 跳过 {c['skipped']} 错误 {c['error']} 合计 {total}")
        try:
            stats = merge_tiles(self.store.tiles_root)
            _log(f"汇总完成: {stats}")
            self.bus.emit(type="log",
                          text=f"已合并到 tiles/latest（新增 {stats['new']} 更新 {stats['updated']} 写入 {stats['copied']}）")
            self.bus.emit(type="merged", stats=stats)
        except Exception as e:
            _log(f"汇总失败: {e}")
            self.bus.emit(type="log", text=f"合并到 latest 失败: {e}")

    # =========================================================
    # 合成（把已有瓦片流式推给浏览器实时拼接；可选保存总图）
    # =========================================================

    def start_compose(self, map_name: str, zoom: str, save: bool = False) -> dict:
        if not valid_map_zoom(map_name, zoom):
            return {"ok": False, "error": "非法地图名/zoom"}
        d = self.store.resolve_dir(map_name, zoom)
        if not d:
            return {"ok": False, "error": f"找不到瓦片目录: {map_name}/{zoom}"}
        with self._lock:
            if self._compose and self._compose.get("running"):
                return {"ok": False, "error": "合成已在运行中"}
            # 取"合并后的瓦片全集"：抓取中的会话只是增量，必须和历史目录并起来
            tiles = list(self.store.iter_tiles(map_name, zoom))
            if not tiles:
                return {"ok": False, "error": f"找不到可用瓦片: {map_name}/{zoom}（{d}）"}
            self._compose = {"map": map_name, "zoom": zoom, "total": len(tiles),
                             "done": 0, "running": True, "dir": str(d)}
            t = threading.Thread(target=self._compose_run,
                                 args=(map_name, zoom, list(tiles), bool(save)),
                                 name="tile-compose", daemon=True)
            t.start()
        self.bus.emit(type="compose_start", map=map_name, zoom=zoom,
                      total=len(tiles), dir=str(d))
        return {"ok": True, "map": map_name, "zoom": zoom, "total": len(tiles)}

    def _compose_run(self, map_name: str, zoom: str, tiles, save: bool):
        comp = self._compose
        # 必须在 try 外先绑定：finally 里要用它（异常发生在下面赋值之前时，
        # 在 finally 里引用未绑定的名字会抛 NameError，把真实错误盖掉）
        file_rel = None
        try:
            xs = [t[0] for t in tiles]
            ys = [t[1] for t in tiles]
            min_x, max_x, min_y, max_y = min(xs), max(xs), min(ys), max(ys)
            self.bus.emit(type="compose_bounds", map=map_name, zoom=zoom,
                          minX=min_x, maxX=max_x, minY=min_y, maxY=max_y)

            # 画布按边界一次建好，不能等"第一张瓦片"再建：第一张读取失败会走
            # `except OSError: continue`，那个 resize 分支永远到不了，画布就停在
            # 1x1；后续瓦片 paste 到 1x1 上被静默裁掉，最后照样保存，把已经合成
            # 好的总图覆盖成一张 1x1 黑图。这是静默丢数据，比合成失败严重得多。
            canvas = None
            if save and Image:
                canvas = Image.new(
                    "RGB",
                    ((max_x - min_x + 1) * TILE_SIZE, (max_y - min_y + 1) * TILE_SIZE),
                    (0, 0, 0))
            pasted = 0

            for i, (x, y, path) in enumerate(tiles, 1):
                try:
                    data = path.read_bytes()
                except OSError as e:
                    self.bus.emit(type="log", text=f"瓦片读取失败 {path.name}: {e}")
                    continue
                if canvas is not None:
                    try:
                        tile_img = Image.open(BytesIO(data)).convert("RGB")
                        canvas.paste(tile_img, ((x - min_x) * TILE_SIZE,
                                                (y - min_y) * TILE_SIZE))
                        pasted += 1
                    except Exception as e:
                        self.bus.emit(type="log", text=f"贴图失败 {path.name}: {e}")
                self.bus.emit(type="tile", map=map_name, zoom=zoom, x=x, y=y,
                              kind="existing", seq=i)
                if comp:
                    comp["done"] = i
                if i % 25 == 0:
                    self.bus.emit(type="compose_progress", map=map_name, zoom=zoom,
                                  done=i, total=len(tiles))

            if canvas is not None and pasted:
                # 按地图/zoom 分类存放，同名覆盖，只保留一份
                out = (self.store.tiles_root / "maps" / map_name / zoom
                       / f"{map_name}_{zoom}.png")
                out.parent.mkdir(parents=True, exist_ok=True)
                # 先写临时文件再原子替换：并发的"下载总图"和缩略图生成不会
                # 读到写了一半的 PNG
                tmp = _unique_tmp(out)
                with _path_lock(out):
                    try:
                        # 必须显式给格式：临时名以 .tmp 结尾，Pillow 无法从扩展名推断
                        canvas.save(tmp, "PNG")
                        _replace_with_retry(tmp, out)
                    except Exception:
                        tmp.unlink(missing_ok=True)
                        raise
                file_rel = f"{map_name}/{zoom}/{out.name}"
                self.bus.emit(type="log",
                              text=f"已保存拼接总图: tiles/maps/{file_rel}（覆盖旧版，只保留一份）")
                # 顺手把缩略图也生成掉：此时整张 canvas 已经在内存里，只多花零点几秒
                # 编码，而"打开地图"那条路就再也不用去解码这张几十 MB 的 PNG 了
                # （实测 map02@4 冷生成要 4.6 秒，其中 3.9 秒是解码）。
                # canvas 到这里已经保存完毕、后面不再使用，所以可以就地缩小。
                try:
                    ov = self.overview_path(map_name, zoom)
                    if ov is not None:
                        ow, oh = _save_overview(canvas, ov)
                        self.bus.emit(type="log",
                                      text=f"已生成缩略图 {ov.name} ({ow}x{oh})")
                except Exception as e:
                    # 缩略图失败不影响"总图已经存好"这件事，只记一行日志
                    _log(f"缩略图生成失败 {map_name}/{zoom}: {e}")
                    self.bus.emit(type="log", text=f"缩略图生成失败: {e}")
            elif canvas is not None:
                self.bus.emit(type="log",
                              text=f"一张瓦片都没贴成功（共 {len(tiles)} 张读取/解码失败），"
                                   "跳过保存，未覆盖已有总图")
            elif save:
                self.bus.emit(type="log",
                              text="未保存总图文件：缺少 Pillow（浏览器端实时拼接不受影响）")
        except Exception as e:
            _log(f"合成异常: {e}")
            self.bus.emit(type="log", text=f"合成异常: {e}")
        finally:
            if comp:
                comp["running"] = False
                comp["done"] = comp["total"]
            self.bus.emit(type="compose_done", map=map_name, zoom=zoom,
                          total=len(tiles), file=file_rel)

    # =========================================================
    # 模拟抓取（不落盘，纯内存演示实时贴图）
    # =========================================================

    def start_simulate(self) -> dict:
        with self._lock:
            if self._simulate and self._simulate.get("running"):
                return {"ok": False, "error": "模拟已在运行中"}
            if self._fetch_thread and self._fetch_thread.is_alive():
                return {"ok": False, "error": "真实抓取运行中，请先停止抓取"}
            self._simulation_cancel.clear()
            self._simulate = {"map": SIM_MAP, "zoom": SIM_ZOOM, "running": True,
                              "total": SIM_COLS * SIM_ROWS, "done": 0}
            t = threading.Thread(target=self._simulate_run, name="tile-sim", daemon=True)
            t.start()
        return {"ok": True}

    def stop_simulate(self) -> dict:
        if self._simulate and self._simulate.get("running"):
            self._simulation_cancel.set()
            return {"ok": True}
        return {"ok": False, "error": "模拟未在运行"}

    def _simulate_run(self):
        # 5列x4行 = 20 个色块区（每块 2x2 瓦片），HSV 均匀取色
        import colorsys
        palette = []
        for i in range(20):
            h = (i * 137) % 360
            r, g, b = colorsys.hsv_to_rgb(h / 360.0, 0.45, 0.55)
            palette.append(tuple(int(v * 255) for v in (r, g, b)))
        n = 0
        try:
            for y in range(SIM_ROWS):
                for x in range(SIM_COLS):
                    if self._simulation_cancel.is_set():
                        return
                    base = palette[(x // 2) + (y // 2) * 5]
                    shade = ((x * 7 + y * 13) % 17) - 8
                    rgb = tuple(max(0, min(255, c + shade)) for c in base)
                    data = solid_png(TILE_SIZE, TILE_SIZE, rgb)
                    n += 1
                    self._sim_tiles[(x, y)] = data
                    self.bus.emit(type="tile", map=SIM_MAP, zoom=SIM_ZOOM,
                                  x=x, y=y, kind="sim", seq=n, size=len(data))
                    if self._simulate:
                        self._simulate["done"] = n
                    time.sleep(SIM_DELAY)
        finally:
            if self._simulate:
                self._simulate["running"] = False
            self.bus.emit(type="simulate_done", map=SIM_MAP, zoom=SIM_ZOOM, total=n)
            self.bus.emit(type="log", text=f"模拟抓取结束，共 {n} 张瓦片（未写盘）")

    # =========================================================
    # 地图标记抓取（只走认证口径）
    # =========================================================
    # 与命令行 `nav-grid-editor fetch-marks` 共用 services.marks，
    # 网页只是换个触发方式，产出与统计口径完全一致。
    # 公开口径已于 2026-09-13 整体退场（见 marks.py 模块头）。

    def marks_status(self) -> dict:
        """给前端的状态快照（含凭证文件是否就位，便于提示）。"""
        p = marks_mod.hg_content_path()
        return {
            "running": bool(self._marks and self._marks.get("running")),
            "has_thread": self._marks_thread is not None and self._marks_thread.is_alive(),
            "stats": self._marks.get("stats") if self._marks else None,
            "error": self._marks.get("error") if self._marks else None,
            "credential_file": str(p),
            "credential_ready": bool(marks_mod.read_hg_content()),
            "data_dir": str(marks_mod.marks_auth_dir(default_assets_dir(self.data_root))),
        }

    def start_marks(self) -> dict:
        """抓认证标记（含玩家自建的滑索/暗管/供电桩等）。"""
        with self._lock:
            if self._marks_thread and self._marks_thread.is_alive():
                return {"ok": False, "error": "标记抓取已在运行中"}
            if not marks_mod.read_hg_content():
                p = marks_mod.hg_content_path()
                return {"ok": False,
                        "error": f"找不到凭证：请把 hg/check 响应的 data.content 粘到 {p} "
                                 "（该文件已 gitignore）"}
            self._marks = {"running": True, "stats": None, "error": None}
            self._marks_thread = threading.Thread(
                target=self._marks_run, name="marks-auth", daemon=True)
            self._marks_thread.start()
        self.bus.emit(type="marks_start")
        self.bus.emit(type="log",
                      text="开始抓取地图标记（认证口径，含玩家自建结构）")
        return {"ok": True}

    def _marks_run(self):
        def log(msg: str):
            _log(f"[marks] {msg}")
            self.bus.emit(type="log", text=msg)

        assets = default_assets_dir(self.data_root)
        try:
            content = marks_mod.read_hg_content()
            if not content:
                raise RuntimeError("凭证文件为空或读不到")
            stats = marks_mod.fetch_auth(marks_mod.marks_auth_dir(assets),
                                         content, log=log)
            warnings = marks_mod.validate(
                stats,
                marks_mod.load_json(marks_mod.marks_auth_dir(assets) / "summary.json") or {},
                None,
            )
            self._marks = {"running": False, "stats": stats,
                           "error": None, "warnings": warnings}
            self.bus.emit(type="marks_done", stats=stats, warnings=warnings)
            self.bus.emit(type="log", text=f"标记抓取完成：{stats.get('maps')} 张图 / "
                                           f"{stats.get('items')} 个物品名 / "
                                           f"{stats.get('points')} 个点位")
        except Exception as e:  # noqa: BLE001 — 线程里必须兜住，否则前端只看到"运行中"
            _log(f"标记抓取失败: {e}")
            self._marks = {"running": False, "stats": None, "error": str(e)}
            self.bus.emit(type="marks_done", stats=None, error=str(e))
            self.bus.emit(type="log", text=f"标记抓取失败: {e}")

    # =========================================================
    # 坐标中继（wsserver/main.py 功能的网页化）
    # =========================================================

    def relay_coords(self, raw_body: bytes) -> dict:
        """POST /api/coords：把游戏/外部客户端上报的坐标广播给所有页面"""
        try:
            data = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except Exception as e:
            return {"ok": False, "error": f"JSON 解析失败: {e}"}
        # 兼容 {"data": {"pos": ...}} 与 {"pos": ...} 两种格式，拍平成一层
        if isinstance(data, dict) and "data" in data:
            data = data["data"]
        self.bus.emit(type="coords", data=data)
        return {"ok": True, "clients": self.bus.subscriber_count}

    # ---------- 已保存总图信息（供前端下拉选择时瞬时显示整图） ----------
    def saved_map_info(self, map_name: str, zoom: str) -> dict | None:
        """返回已保存总图的相对路径与瓦片边界/数量；没有则返回 None"""
        if not valid_map_zoom(map_name, zoom):
            return None
        p = self.store.tiles_root / "maps" / map_name / zoom / f"{map_name}_{zoom}.png"
        if not p.is_file():
            return None
        # 边界必须按"合并后的瓦片全集"算：前端拿这组边界去贴已保存的总图，
        # 只统计单个目录（比如抓取中的会话）会得到缩小的范围、图就对不上。
        xs, ys = [], []
        for x, y, _ in self.store.iter_tiles(map_name, zoom):
            xs.append(x)
            ys.append(y)
        return {
            "file": f"{map_name}/{zoom}/{p.name}",
            "minX": min(xs) if xs else 0,
            "maxX": max(xs) if xs else 0,
            "minY": min(ys) if ys else 0,
            "maxY": max(ys) if ys else 0,
            "count": len(xs),
        }

    def overview_path(self, map_name: str, zoom: str) -> Path | None:
        """缩略总图的缓存文件路径；没有总图时返回 None。"""
        if not valid_map_zoom(map_name, zoom):
            return None
        src = self.store.tiles_root / "maps" / map_name / zoom / f"{map_name}_{zoom}.png"
        if not src.is_file():
            return None
        return src.with_name(f"{map_name}_{zoom}{OVERVIEW_SUFFIX}")

    def ensure_overview(self, map_name: str, zoom: str) -> Path | None:
        """生成/复用缩略总图，返回缓存文件路径。

        前端以前是直接把总图整张下下来再画到同尺寸画布上：map02@4 的文件
        78.9 MB，解码后是 10752x15872 的位图，约 685 MB。这是"打开地图就卡"
        的主因。缩略图按 2048 长边生成后只有一两百 KB，缩着看完全够用；
        放大到瓦片级时前端另外按需取瓦片。

        **正常情况下这个函数不用干活**：`_compose_run` 存总图时顺手就把缩略图
        生成了（那时整张 canvas 已经在内存里，只多花零点几秒）。这里只是兜底：
        直接删掉缩略图、或总图是别处产生的，才需要重新解码那张几十 MB 的 PNG
        ——实测 map02@4 冷生成要 4.6 秒。
        """
        out = self.overview_path(map_name, zoom)
        if out is None or Image is None:
            return None
        src = self.store.tiles_root / "maps" / map_name / zoom / f"{map_name}_{zoom}.png"
        try:
            if out.is_file() and out.stat().st_mtime >= src.stat().st_mtime:
                return out
            t0 = time.perf_counter()
            img = Image.open(src)
            if img.mode != "RGB":
                img = img.convert("RGB")
            size = _save_overview(img, out)
            _log(f"缩略总图已生成 {out.name} ({size[0]}x{size[1]})"
                 f" 用时 {time.perf_counter() - t0:.1f}s")
            return out
        except Exception as e:
            _log(f"缩略总图生成失败 {src}: {e}")
            return None

    # =========================================================
    # 地图标定：建立 像素 ↔ 游戏坐标 的仿射坐标系
    # （思路与格式兼容 wsserver/map_calibrator.py，纯 Python 实现，无 numpy 依赖）
    # =========================================================

    def calib_file(self, map_name: str, zoom: str) -> Path | None:
        """标定文件位置：新结构 maps/<地图>/<zoom>/<地图>_<zoom>_mapping.json，
        兼容旧平铺 maps/<地图>_<zoom>_mapping.json"""
        if not valid_map_zoom(map_name, zoom):
            return None
        nested = self.store.tiles_root / "maps" / map_name / zoom / f"{map_name}_{zoom}_mapping.json"
        if nested.is_file():
            return nested
        legacy = self.store.tiles_root / "maps" / f"{map_name}_{zoom}_mapping.json"
        if legacy.is_file():
            return legacy
        return None

    def get_calib(self, map_name: str, zoom: str) -> dict | None:
        """读取该地图/zoom 的标定（mapping）数据"""
        p = self.calib_file(map_name, zoom)
        if not p:
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            _log(f"标定文件解析失败 {p}: {e}")
            return None

    def save_calib(self, map_name: str, zoom: str, points: list,
                   image_size: dict, threshold: float = 5.0) -> dict:
        """保存标定：控制点（像素+游戏坐标）→ 仿射矩阵 + 逆矩阵 + 误差。

        points: [{"pixel": [px, py], "world": [x, z], "enabled": bool}]
        返回完整 mapping 数据（已写盘）。
        """
        if not valid_map_zoom(map_name, zoom):
            return {"ok": False, "error": "非法地图名/zoom"}
        if not isinstance(points, list):
            return {"ok": False, "error": "points 必须是数组"}
        if not points or len(points) < 3:
            return {"ok": False, "error": "至少需要 3 个启用中的控制点"}
        pixel, world, metas = [], [], []
        for p in points:
            # 每个点都可能是任意 JSON：不是对象、坐标不是两个数字都要在这里
            # 变成明确的错误返回。漏出去的话 AttributeError/ValueError 会直接
            # 冒到 do_POST——那边没有兜底，浏览器只会看到连接被断开。
            if not isinstance(p, dict):
                return {"ok": False,
                        "error": "控制点必须是对象 {pixel:[px,py], world:[x,z]}"}
            try:
                px, py = p.get("pixel", [None, None])
                wx, wz = p.get("world", [None, None])
                if None in (px, py, wx, wz):
                    continue
                px, py = float(px), float(py)
                wx, wz = float(wx), float(wz)
            except (TypeError, ValueError, OverflowError):
                return {"ok": False, "error": "控制点的 pixel/world 必须是两个数字"}
            # 非有限坐标会算出一个 NaN 矩阵写进 mapping.json，而 json.loads 照单
            # 全收，之后每次像素↔世界换算都静默变成 nan（与网格路径同一个坑）
            if not all(math.isfinite(v) for v in (px, py, wx, wz)):
                return {"ok": False, "error": "控制点坐标必须是有限数"}
            # 还要限量级：RANSAC 的距离和逐点误差都要平方，1e200 平方就溢出成
            # OverflowError，而那是从拟合内部抛出来的，位置比 ValueError 更隐蔽
            if max(abs(px), abs(py), abs(wx), abs(wz)) > 1e12:
                return {"ok": False, "error": "控制点坐标超出合理范围"}
            pixel.append([px, py])
            world.append([wx, wz])
            metas.append(bool(p.get("enabled", True)))
        if sum(metas) < 3:
            return {"ok": False, "error": "至少需要 3 个启用中的控制点"}

        # 只用启用点参与拟合；RANSAC 剔除离群点，再求最小二乘
        use_pixel = [pixel[i] for i in range(len(pixel)) if metas[i]]
        use_world = [world[i] for i in range(len(world)) if metas[i]]
        try:
            matrix, inlier_flags = _fit_affine_ransac(use_pixel, use_world, threshold)
            # 求逆也要留在 try 里：控制点共线或世界坐标重复时仿射退化，
            # _invert_affine 抛的 ValueError 漏出去就是一个没有 JSON 的 500
            inverse_matrix = _invert_affine(matrix)
        except (ValueError, OverflowError) as e:
            return {"ok": False, "error": f"拟合失败: {e}"}

        # 逐点误差（用最终矩阵对全部点算）
        errs = []
        for i in range(len(pixel)):
            wx, wz = _mat_vec(matrix, pixel[i][0], pixel[i][1])
            errs.append(((wx - world[i][0]) ** 2 + (wz - world[i][1]) ** 2) ** 0.5)
        avg_err = sum(errs) / len(errs) if errs else 0.0
        max_err = max(errs) if errs else 0.0

        control_points = []
        j = 0
        for i in range(len(pixel)):
            inlier = None
            if metas[i]:
                inlier = bool(inlier_flags[j])
                j += 1
            control_points.append({
                "pixel": [pixel[i][0], pixel[i][1]],
                "world": [world[i][0], world[i][1]],
                # enabled 保留**用户勾选**，inlier 单独存拟合结果。以前两者共用
                # enabled 一个字段，重新加载后复选框会变成上一次的 RANSAC 观点，
                # 用户的选择被静默改写，下次拟合用的点集和界面上看到的不是一套。
                "enabled": metas[i],
                "inlier": inlier,
                "error": round(errs[i], 4),
            })

        if not isinstance(image_size, dict):
            image_size = {}
        mapping = {
            "map_name": f"{map_name}_{zoom}.png",
            "image_size": {
                "width": _as_int(image_size.get("width", 0)),
                "height": _as_int(image_size.get("height", 0)),
            },
            "matrix": matrix,
            "inverse_matrix": inverse_matrix,
            "average_error": round(avg_err, 4),
            "max_error": round(max_err, 4),
            "control_points": control_points,
        }
        out = (self.store.tiles_root / "maps" / map_name / zoom
               / f"{map_name}_{zoom}_mapping.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再原子替换：并发的 get_calib 读 text 不会读到写了一半的
        # JSON（读一半会 json 解析失败 -> 编辑器误判成"未标定"）
        tmp = _unique_tmp(out)
        with _path_lock(out):
            try:
                tmp.write_text(json.dumps(mapping, ensure_ascii=False, indent=2),
                               encoding="utf-8")
                _replace_with_retry(tmp, out)
            except Exception as e:
                tmp.unlink(missing_ok=True)
                _log(f"标定文件写入失败 {out}: {e}")
                return {"ok": False, "error": f"标定文件写入失败: {e}"}
        mapping["ok"] = True
        mapping["saved"] = str(out.relative_to(self.store.tiles_root))
        return mapping

    def world_to_pixel(self, map_name: str, zoom: str, x: float, z: float):
        """游戏坐标 (x, z) → 地图图像素 (px, py)；未标定返回 None"""
        m = self.get_calib(map_name, zoom)
        if not m or "inverse_matrix" not in m:
            return None
        inv = m["inverse_matrix"]
        px = inv[0][0] * x + inv[0][1] * z + inv[0][2]
        py = inv[1][0] * x + inv[1][1] * z + inv[1][2]
        return (px, py)

    # =========================================================
    # 2D 网格 —— 稠密 uint8 npz，供 ok-end-field 直接消费
    # =========================================================

    def grid2d_path(self, map_name: str, zoom: str) -> Path:
        """2D 网格的落盘位置。

        单一位置，不再有"读哪写哪"的旧位置兼容分支：新格式文件名是
        <地图>_<zoom>.grid.npz（没有 _2d 中缀）。
        """
        return self.grid2d_dir / f"{map_name}_{zoom}{GRID_SUFFIX}"

    def get_grid2d(self, map_name: str, zoom: str) -> dict:
        """读取 2D 网格 npz，把稠密数组转回**稀疏列表**交给前端。

        线格式：cells/blocked 两个 [ix,iz] 列表 + origin + cell_size + shape。
        前端的像素↔格子换算依赖"下标 0 ⇔ origin"这一关系；npz 的 origin 定义
        就是 cells[0,0] 最小角的世界坐标，二者一致，几何数学无需改动。

        shape 是数组范围。前端不能只看已涂格子：带未知边框的网格（别的工具
        写出的、或扩过范围的）如果只按已涂格子重建，保存时会把边框静默裁掉。

        校验与 ok-end-field 的 grid_io.load_grid / scripts/nav/verify_grid.py
        同级——读方会拒的文件这里也必须拒，不能静默按"未知格"画出来。
        """
        # 与 save_grid2d 同样的入参校验：grid2d_path 是拼字符串建路径，
        # map_name 里带 / 或 .. 就能读到 nav-grid-data/ 之外的同格式文件。
        if not valid_map_zoom(map_name, zoom):
            return {"data": None, "source": None, "error": "非法地图名/zoom"}
        p = self.grid2d_path(map_name, zoom)
        if not p.is_file():
            return {"data": None, "source": None}
        try:
            arr, meta, warnings = _load_grid_npz(p)
        except Exception as e:
            _log(f"2D 网格读取失败 {p}: {e}")
            return {"data": None, "source": None, "error": str(e)}

        # 稠密下标 (i=行=z, j=列=x) → 前端的 [ix, iz]（ix 沿 x）
        cells = [[int(j), int(i)] for i, j in np.argwhere(arr == CELL_FREE)]
        blocked = [[int(j), int(i)] for i, j in np.argwhere(arr == CELL_BLOCKED)]
        if str(meta.get("map_name") or "") != map_name or str(meta.get("zoom") or "") != zoom:
            warnings.append(f"meta 里的 map_name/zoom 与文件名不一致"
                            f"（{meta.get('map_name')!r}/{meta.get('zoom')!r}）")
        touched = len(cells) + len(blocked)
        if touched > EDIT_CELL_LIMIT:
            warnings.append(f"已涂格子 {touched} 超过编辑器上限 {EDIT_CELL_LIMIT}，"
                            "只能查看，保存会被拒绝")
        return {
            "data": {
                "origin": [float(v) for v in meta["origin"]],
                "cell_size": float(meta["cell_size"]),
                "shape": [int(arr.shape[0]), int(arr.shape[1])],
                "cells": cells,
                "blocked": blocked,
            },
            "source": str(p),
            "warnings": warnings,
        }

    def save_grid2d(self, map_name: str, zoom: str, data: dict) -> dict:
        """把编辑器里稀疏的 free/blocked 落成稠密 uint8 npz。

        入参仍是稀疏列表（与前端线格式一致），这里做稠密化。最终范围取
        **原数组范围（data["shape"]，读文件时带回来的）∪ 已涂格子范围**，
        盒内未触碰处填 CELL_UNKNOWN；origin 随之平移到 cells[0,0] 最小角。

        - 新建网格（没有 shape）→ 按已涂格子的边界盒定范围，与旧行为一致。
        - 打开已有网格 → 未知边框不会被静默裁掉；涂到范围外的格子会自动扩图。
        没有这一步的话，"读进来再存回去"对一个带未知边框的网格是有损的：
        编辑器只看得到已涂格子，重建时会把没人画过的边框整圈丢掉。

        data: {"origin": [x, y, z], "cell_size": n, "shape": [h, w]（可选）,
               "cells": [[ix,iz]...], "blocked": [...]}
        origin[1]（y）只作格式占位，固定写 0：2D 网格不带高度。
        """
        if not valid_map_zoom(map_name, zoom):
            return {"ok": False, "error": "非法地图名/zoom"}
        try:
            ov = data["origin"]
            if len(ov) == 2:  # 兼容 [x, z]，自动补 y=0
                origin = [float(ov[0]), 0.0, float(ov[1])]
            elif len(ov) == 3:
                origin = [float(ov[0]), float(ov[1]), float(ov[2])]
            else:
                raise ValueError("origin 必须是 [x, y, z] 或 [x, z]")
            cell_size = float(data["cell_size"])
            # isfinite 一并挡掉 NaN/inf：`float('inf') > 0` 是 True，只比大小拦不住。
            # 放过去的话 json.dumps 会写出非标准的 Infinity/NaN 字面量，读方
            # json.loads 也接受，于是规划器拿到 extent=(nan,nan,nan,nan) 却算
            # "读入成功"——正是 README 承诺不会发生的那种静默错位。
            if not math.isfinite(cell_size) or cell_size <= 0:
                raise ValueError("cell_size 必须是有限正数")
            if not all(math.isfinite(v) for v in origin):
                raise ValueError("origin 必须都是有限数")
        except (KeyError, TypeError, ValueError, IndexError, OverflowError):
            return {"ok": False, "error": "origin/cell_size 缺失或非法（必须是有限数）"}

        # 原数组范围（下标空间与 cells/blocked 相同：下标 0 ⇔ origin 那格的最小角）
        raw_shape = data.get("shape")
        base_h = base_w = 0
        if raw_shape is not None:
            try:
                base_h, base_w = int(raw_shape[0]), int(raw_shape[1])
            except (TypeError, ValueError, IndexError, KeyError):
                return {"ok": False, "error": "shape 必须是 [height, width] 两个整数"}
            if base_h <= 0 or base_w <= 0:
                return {"ok": False, "error": f"shape 的宽高必须为正，实际 {raw_shape!r}"}

        cells, blocked = [], []
        for name in ("cells", "blocked"):
            arr = data.get(name, [])
            if not isinstance(arr, list):
                return {"ok": False, "error": f"{name} 必须是数组"}
            for c in arr:
                try:
                    if len(c) != 2:
                        return {"ok": False, "error": f"{name} 坐标必须是两个整数（无高度）"}
                    c = [int(c[0]), int(c[1])]
                except (TypeError, ValueError, IndexError):
                    return {"ok": False, "error": f"{name} 坐标必须是两个整数"}
                if not (-1_000_000 <= c[0] <= 1_000_000 and -1_000_000 <= c[1] <= 1_000_000):
                    return {"ok": False, "error": f"{name} 坐标越界"}
            if name == "cells":
                cells = arr
            else:
                blocked = arr
        if len(cells) + len(blocked) > EDIT_CELL_LIMIT:
            return {"ok": False, "error": f"格子数量超过 {EDIT_CELL_LIMIT} 上限"}

        cells = [(int(c[0]), int(c[1])) for c in cells]
        blocked = [(int(c[0]), int(c[1])) for c in blocked]
        touched = cells + blocked
        if base_h and base_w:
            # 原范围 ∪ 已涂格子范围：只扩不缩，未知边框得以保留
            xs = [c[0] for c in touched]
            zs = [c[1] for c in touched]
            min_ix, max_ix = min([0] + xs), max([base_w - 1] + xs)
            min_iz, max_iz = min([0] + zs), max([base_h - 1] + zs)
        elif touched:
            # 新建网格：边界盒即范围（旧行为）
            min_ix = min(c[0] for c in touched)
            max_ix = max(c[0] for c in touched)
            min_iz = min(c[1] for c in touched)
            max_iz = max(c[1] for c in touched)
        else:
            return {"ok": False,
                    "error": "网格是空的：既没有已涂格子也没有原范围（shape），无法确定写多大"
                             "（要清空请直接删掉 nav-grid-data/ 下的文件）"}
        width = max_ix - min_ix + 1   # 列数 = x 方向
        height = max_iz - min_iz + 1  # 行数 = z 方向
        if width * height > DENSE_CELL_LIMIT:
            # 带上世界坐标范围：跨度炸了基本只有两种可能——地图本来就大（那就该
            # 调大 cell_size，用更粗的格子覆盖），或者某一笔涂到了很远的坐标上，
            # 看范围就能分辨是哪一种。
            return {"ok": False,
                    "error": f"格子跨度 {width}×{height}={width * height} 超过稠密化上限"
                             f"（{DENSE_CELL_LIMIT} 格）。世界范围 x "
                             f"{origin[0] + min_ix * cell_size:.0f}..{origin[0] + max_ix * cell_size:.0f}、"
                             f"z {origin[2] + min_iz * cell_size:.0f}..{origin[2] + max_iz * cell_size:.0f}；"
                             "要么把 cell_size 调大（格子更粗、覆盖同样范围用的格子更少），"
                             "要么把涂到远处的格子擦掉"}

        # 行=iz、列=ix（必须与 ok-end-field 的 i=row=z / j=col=x 一致，否则网格会转置）
        grid = np.full((height, width), CELL_UNKNOWN, dtype=np.uint8)
        for ix, iz in cells:
            grid[iz - min_iz, ix - min_ix] = CELL_FREE
        # blocked 后写：同一格既标 Free 又标 Blocked 时以 Blocked 为准（保守，稠密格式存不下两义）
        for ix, iz in blocked:
            grid[iz - min_iz, ix - min_ix] = CELL_BLOCKED

        meta = {
            "magic": GRID_MAGIC,
            "schema_version": GRID_SCHEMA_VERSION,
            "map_name": map_name,
            "zoom": zoom,
            # 平移成 cells[0,0] 最小角的世界坐标
            "origin": [origin[0] + min_ix * cell_size, 0.0, origin[2] + min_iz * cell_size],
            "cell_size": cell_size,
            "axis_convention": GRID_AXIS_CONVENTION,
            "source": "nav-grid-editor",
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        out = self.grid2d_path(map_name, zoom)
        existed = out.is_file()
        out.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再原子替换：服务是多线程的，两次保存撞上会写出半个 npz。
        # 临时名必须每次唯一（见 _unique_tmp）：固定叫 <name>.tmp 时两个并发保存
        # 会抢同一个文件，先完成的一方 os.replace 之后另一方就 FileNotFoundError；
        # 更糟的时序是自检读到对方写了一半的内容，把残缺文件替换成正式产物。
        tmp = _unique_tmp(out)
        with _path_lock(out):
            try:
                with open(tmp, "wb") as fh:
                    # meta 用 numpy 字符串数组存（0 维），读方才能不开 allow_pickle
                    np.savez_compressed(fh, cells=grid,
                                        meta=np.array(json.dumps(meta, ensure_ascii=False)))
                # 导出自检：用与 ok-end-field 读方同级的检查读回临时文件，通过才替换。
                # 宁可保存失败也不能覆盖成读不回来的文件。
                _load_grid_npz(tmp)
                _replace_with_retry(tmp, out)
            except Exception as e:
                tmp.unlink(missing_ok=True)
                _log(f"2D 网格导出失败 {out}: {e}")
                return {"ok": False, "error": f"导出失败（未覆盖原文件）: {e}"}
        return {"ok": True, "saved": out.name, "path": str(out),
                "dir": str(out.parent), "overwrote": existed,
                "shape": [height, width],
                "origin": list(meta["origin"]),
                # 服务端按"原范围 ∪ 已涂格子"重定了下标基准，前端要把内存里的
                # 格子坐标回移这么多，才能保持"下标 0 ⇔ origin"这一不变量
                "shift": [min_ix, min_iz]}

# =========================================================
# 2D 网格读取：与 ok-end-field 的读方同级校验
# =========================================================
