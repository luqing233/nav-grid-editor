# -*- coding: utf-8 -*-
"""地图瓦片、合成、标定和 2D 网格的业务服务。"""

from __future__ import annotations

import json
import hashlib
import math
import os
import queue
import threading
import time
import urllib.request
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import numpy as np

from .. import marks as marks_mod
from ..endfield_ws import EndfieldPositionClient
from .calibration import _fit_affine_ransac, _invert_affine, _mat_vec
from .events import EventBus
from .grid import _load_grid_meta_npz, _load_grid_npz
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
from .layers import (
    CATALOG_FILENAME,
    MAX_LAYER_IMAGE_BYTES,
    build_level_index,
    levels_index_path,
    load_level_index,
    write_level_index,
    builtin_layer_entries,
    find_layer,
    layer_cache_relpath,
    layer_source_url,
    load_layer_catalog,
    list_layers as list_layer_catalog,
)
from .links import (
    LINK_SCHEMA_VERSION,
    delete_link,
    links_path,
    load_links,
    normalize_link,
    upsert_link,
)
from .settings import (
    MAIN_TIER_ID,
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
    disk_tier,
    api_tier,
    valid_map_zoom,
    valid_grid_tier,
)
from .tiles import TileStore, merge_tiles


_MAX_FLOOR_REGIONS = 256
_MAX_FLOOR_REGION_CELLS = 50_000
_GRID_ARRAY_CACHE_MAX = 4
#: 主图的 tier 在 API 口径下是 __main__、落盘口径下是空串。两者只经
#: settings.disk_tier / api_tier 转换，别再内联判断。
MAIN_GRID_KEY = MAIN_TIER_ID
_LAYER_HEADERS = {
    "Referer": MAP_URL,
    "User-Agent": "Mozilla/5.0 nav-grid-editor",
}


def _normalize_floor_regions(raw) -> list[dict]:
    """Validate optional world-space 3D regions stored in a grid's metadata."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("floor_regions 必须是数组")
    if len(raw) > _MAX_FLOOR_REGIONS:
        raise ValueError(f"floor_regions 不能超过 {_MAX_FLOOR_REGIONS} 个")
    out = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"floor_regions[{index}] 必须是对象")
        try:
            x0 = float(item["x0"])
            x1 = float(item["x1"])
            z0 = float(item["z0"])
            z1 = float(item["z1"])
            y_min = float(item["y_min"])
            y_max = float(item["y_max"])
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ValueError(
                f"floor_regions[{index}] 需要 x0/x1/z0/z1/y_min/y_max") from None
        values = (x0, x1, z0, z1, y_min, y_max)
        if not all(math.isfinite(value) for value in values):
            raise ValueError(f"floor_regions[{index}] 含有非有限数")
        if x0 > x1 or z0 > z1 or y_min > y_max:
            raise ValueError(f"floor_regions[{index}] 的最小值不能大于最大值")
        region = {
            "x0": x0, "x1": x1, "z0": z0, "z1": z1,
            "y_min": y_min, "y_max": y_max,
        }
        component_id = str(item.get("component_id") or "").strip()
        if component_id:
            region["component_id"] = component_id
        raw_cells = item.get("cells")
        if raw_cells is not None:
            if not isinstance(raw_cells, list):
                raise ValueError(f"floor_regions[{index}].cells 必须是数组")
            cells = []
            for cell in raw_cells:
                if not isinstance(cell, (list, tuple)) or len(cell) != 4:
                    raise ValueError(
                        f"floor_regions[{index}].cells 每项应为 [x0,x1,z0,z1]")
                try:
                    cell_values = [float(value) for value in cell]
                except (TypeError, ValueError, OverflowError):
                    raise ValueError(
                        f"floor_regions[{index}].cells 含有非法数字") from None
                if not all(math.isfinite(value) for value in cell_values):
                    raise ValueError(
                        f"floor_regions[{index}].cells 含有非有限数")
                if cell_values[0] > cell_values[1] or cell_values[2] > cell_values[3]:
                    raise ValueError(
                        f"floor_regions[{index}].cells 的最小值不能大于最大值")
                cells.append(cell_values)
            if len(cells) > _MAX_FLOOR_REGION_CELLS:
                raise ValueError(
                    f"floor_regions[{index}].cells 不能超过 {_MAX_FLOOR_REGION_CELLS}")
            if cells:
                region["cells"] = cells
        out.append(region)
    return out


def _region_overlap_ratio(region: dict, new_rects: list[list[float]]) -> float:
    """How strongly an existing region overlaps the new component geometry."""
    old_rects = region.get("cells")
    if old_rects:
        old_keys = {
            (
                round((float(rect[0]) + float(rect[1])) / 2.0, 3),
                round((float(rect[2]) + float(rect[3])) / 2.0, 3),
            )
            for rect in old_rects
            if isinstance(rect, (list, tuple)) and len(rect) == 4
        }
        new_keys = {
            (
                round((float(rect[0]) + float(rect[1])) / 2.0, 3),
                round((float(rect[2]) + float(rect[3])) / 2.0, 3),
            )
            for rect in new_rects
        }
        if not old_keys or not new_keys:
            return 0.0
        return len(old_keys & new_keys) / min(len(old_keys), len(new_keys))
    old_area = max(0.0, region["x1"] - region["x0"]) * max(
        0.0, region["z1"] - region["z0"])
    new_x0 = min(rect[0] for rect in new_rects)
    new_x1 = max(rect[1] for rect in new_rects)
    new_z0 = min(rect[2] for rect in new_rects)
    new_z1 = max(rect[3] for rect in new_rects)
    overlap_w = max(
        0.0, min(region["x1"], new_x1) - max(region["x0"], new_x0))
    overlap_h = max(
        0.0, min(region["z1"], new_z1) - max(region["z0"], new_z0))
    new_area = max(0.0, new_x1 - new_x0) * max(0.0, new_z1 - new_z0)
    return (overlap_w * overlap_h) / max(1e-9, min(old_area, new_area))


def _download_layer_image(url: str) -> bytes:
    """Fetch one official layer PNG with a size guard."""
    req = urllib.request.Request(url, headers=_LAYER_HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        if getattr(resp, "status", 200) != 200:
            raise OSError(f"HTTP {resp.status}")
        data = resp.read(MAX_LAYER_IMAGE_BYTES + 1)
    if len(data) > MAX_LAYER_IMAGE_BYTES:
        raise OSError("分层图片超过大小上限")
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise OSError("分层资源不是 PNG")
    return data


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
        # 官方地图 WS 位置流：直接连 wss://ws.skland.com/...，不再依赖本地 3001 中继
        self._position = EndfieldPositionClient(
            on_position=self._on_position,
            on_state=self._on_position_state,
            log=self._position_log,
        )
        self._layer_catalog_cache: tuple[int, list[dict]] | None = None
        self._floor_grid_cache: dict[Path, tuple[int, dict]] = {}
        # 解开的网格数组（判连接端点是否落在可走格上要按格看内容，光有元数据不够）。
        # npz 是整块压缩，读一次就得全解开，map02@4 是 5 MB 上下，所以只留最近几个。
        self._grid_array_cache: dict[Path, tuple[int, tuple]] = {}

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
            "position": self._position.status(),
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
    # 玩家位置：官方地图 WS 客户端 + 坐标中继
    # =========================================================

    def start_position_stream(self) -> dict:
        """启动官方地图 WS 位置流（连 skland，不再依赖本地 ws 服务器）。"""
        res = self._position.start()
        if res.get("started"):
            self.bus.emit(type="position_state", state=self._position.status())
            self.bus.emit(type="log", text="位置流：官方地图 WebSocket 客户端已启动")
        return res

    def stop_position_stream(self) -> dict:
        res = self._position.stop()
        self.bus.emit(type="position_state", state=self._position.status())
        self.bus.emit(type="log", text="位置流：官方地图 WebSocket 客户端已停止")
        return res

    def position_status(self) -> dict:
        return self._position.status()

    def _tier_grid_meta(self, map_name: str, tier: str) -> dict | None:
        if not valid_map_zoom(map_name, "4") or (
                tier != MAIN_GRID_KEY and not valid_grid_tier(tier)):
            return None
        if tier == MAIN_GRID_KEY:
            paths = sorted(
                (path for path in self.grid2d_dir.glob(f"{map_name}_*.grid.npz")
                 if "__" not in path.name),
                key=lambda path: path.name,
                reverse=True,
            )
        else:
            paths = sorted(
                self.grid2d_dir.glob(f"{map_name}__{tier}_*.grid.npz"),
                key=lambda path: path.name,
                reverse=True,
            )
        for path in paths:
            try:
                mtime = path.stat().st_mtime_ns
                cached = self._floor_grid_cache.get(path)
                if cached and cached[0] == mtime:
                    return cached[1]
                meta = _load_grid_meta_npz(path)
                if str(meta.get("map_name") or "") != map_name:
                    continue
                meta_tier = str(meta.get("tier") or "")
                if tier == MAIN_GRID_KEY:
                    if meta_tier:
                        continue
                elif meta_tier != tier:
                    continue
                self._floor_grid_cache[path] = (mtime, meta)
                return meta
            except Exception as e:
                _log(f"读取楼层网格元数据失败 {path}: {e}")
        return None

    @staticmethod
    def _region_contains(region: dict, x: float, y: float, z: float) -> bool:
        cells = region.get("cells")
        if cells:
            inside = any(
                cell[0] <= x <= cell[1] and cell[2] <= z <= cell[3]
                for cell in cells
            )
            if not inside:
                return False
        return (
            region["x0"] <= x <= region["x1"] and
            region["z0"] <= z <= region["z1"] and
            region["y_min"] <= y <= region["y_max"]
        )

    def resolve_tier_by_height(self, map_name: str,
                               x: float, y: float, z: float) -> dict:
        """由三维坐标判定玩家在哪一层 —— **只看坐标，不看 levelId**。

        官方的 `levelId` 不可信（同一个 levelId 会被多个分层共用、也可能滞后于
        玩家实际位置），所以候选层是「这张地图上**所有声明了 `floor_regions` 的
        层**」（主图同样是候选，只要它自己标了区间），命中靠坐标落在区间里。

        没有区间命中时报 `main`（大地图）而不是"不知道" —— 没有分层重叠的地方
        本来就只能是大地图。`reason` 把原因带出来，因为这两种情况在界面上该有
        不同的说明：

        - `no_region_covers`：有层标了区间，但没有一个盖住这点；
        - `no_layers`：这张地图一层都没标过区间（当前数据的常态，map02 只有
          2 段各 39 格，所以绝大多数坐标都走这条兜底 —— 它是主路径，不是边角）。

        返回 ``{status, tier_id, matched, reason}``；`status` 取值
        `unique` / `ambiguous` / `main` / `invalid`。

        `invalid` 单独一档（地图名非法、坐标不是有限数）：那不是"在地表"，是这条
        位置消息本身不可用，调用方**不该**据此把视图切到大地图。
        """
        def fallback(reason: str) -> dict:
            return {"status": "main", "tier_id": "", "matched": [], "reason": reason}

        if not valid_map_zoom(map_name, "4"):
            return {"status": "invalid", "tier_id": "", "matched": [],
                    "reason": "invalid_map"}
        try:
            x, y, z = float(x), float(y), float(z)
        except (TypeError, ValueError, OverflowError):
            return {"status": "invalid", "tier_id": "", "matched": [],
                    "reason": "invalid_position"}
        if not all(math.isfinite(value) for value in (x, y, z)):
            return {"status": "invalid", "tier_id": "", "matched": [],
                    "reason": "invalid_position"}

        entries = []
        for tier in self._grid_tier_ids(map_name, "4"):
            meta = self._nav_tier_meta(map_name, "4", tier)
            if not meta:
                continue
            try:
                regions = _normalize_floor_regions(meta.get("floor_regions") or [])
            except ValueError as e:
                _log(f"网格楼层区间非法 {map_name}__{tier}: {e}")
                regions = []
            if not regions:
                continue
            matched = any(self._region_contains(region, x, y, z)
                          for region in regions)
            entries.append({"tier_id": tier, "matched": matched})

        if not entries:
            return fallback("no_layers")
        matched = [entry["tier_id"] for entry in entries if entry["matched"]]
        if len(matched) == 1:
            return {"status": "unique", "tier_id": api_tier(matched[0]),
                    "matched": [api_tier(t) for t in matched],
                    "reason": "region_hit"}
        if len(matched) > 1:
            # 高度区间真的重叠了 —— 服务端不替调用方挑，交回去让它决定
            return {"status": "ambiguous", "tier_id": "",
                    "matched": [api_tier(t) for t in matched],
                    "reason": "overlap"}
        return fallback("no_region_covers")

    def get_grid_floor_map(self, map_name: str, zoom: str,
                           level_id: str) -> dict:
        """Return floor metadata for every tier in one levelId."""
        if not valid_map_zoom(map_name, zoom):
            return {"ok": False, "error": "非法地图名/zoom"}
        wanted_level = str(level_id or "")
        layers = [
            layer for layer in self.list_layers(map_name, zoom)
            if not wanted_level
            or str(layer.get("level_id") or "") == wanted_level
        ]
        out = []
        main_meta = self._tier_grid_meta(map_name, MAIN_GRID_KEY)
        if main_meta:
            regions = []
            try:
                regions = _normalize_floor_regions(
                    main_meta.get("floor_regions") or [])
            except ValueError as e:
                _log(f"网格楼层区间非法 {map_name}__main: {e}")
            out.append({
                "tier_id": MAIN_GRID_KEY,
                "name": "大地图",
                "group": "主地图",
                "bounds": {},
                "floor_regions": regions,
                "has_grid": True,
            })
        for layer in layers:
            tier = str(layer.get("id") or "")
            meta = self._tier_grid_meta(map_name, tier)
            regions = []
            if meta:
                try:
                    regions = _normalize_floor_regions(
                        meta.get("floor_regions") or [])
                except ValueError as e:
                    _log(f"网格楼层区间非法 {map_name}__{tier}: {e}")
            out.append({
                "tier_id": tier,
                "name": layer.get("name") or tier,
                "group": layer.get("group") or "",
                "bounds": dict(layer.get("bounds") or {}),
                "floor_regions": regions,
                "has_grid": bool(meta),
            })
        return {"ok": True, "map": map_name, "zoom": zoom,
                "level_id": level_id, "layers": out}

    def sync_level_index(self) -> dict:
        """把官方分层目录物化成 `nav-grid-data/levels.json`。

        消费侧枚举「某个 level_id 声明了哪些层」时只能靠这份文件 —— 分层目录
        （catalog.json）是 fetch-layers 的可重生缓存、不进版本库，而按网格文件反推
        会把不同 level_id 的层混成一批。

        **只在分层目录真的存在时才写**：目录缺失时 `_layer_entries()` 会退回内置的
        Map02Lv002 兜底目录（7 条），拿它去覆盖已提交的完整索引就是数据丢失。
        """
        catalog = self.store.tiles_root / "layers" / CATALOG_FILENAME
        if not catalog.is_file():
            return {"ok": False, "error": "没有分层目录（先跑 fetch-layers）"}
        try:
            entries = load_layer_catalog(catalog)
        except Exception as e:
            return {"ok": False, "error": f"分层目录读取失败: {e}"}
        doc = build_level_index(entries)
        path = levels_index_path(self.grid2d_dir)
        # 内容没变就不写：这份文件在版本库里，每次保存网格都刷新时间戳会一直脏。
        existing = load_level_index(path)
        if existing is not None and existing.get("layers") == doc["layers"]:
            return {"ok": True, "path": str(path), "count": len(doc["layers"]),
                    "unchanged": True}
        doc["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        write_level_index(path, doc)
        return {"ok": True, "path": str(path), "count": len(doc["layers"]),
                "unchanged": False}

    def get_level_index(self) -> dict:
        """读层声明索引（消费侧/界面用）。没有就现算一份，不落盘。"""
        path = levels_index_path(self.grid2d_dir)
        existing = load_level_index(path)
        if existing is not None:
            return existing
        return build_level_index(self._layer_entries())

    def _tier_grid_candidates(self, map_name: str, tier: str) -> list[Path]:
        """某层现有的全部网格文件，按文件名倒序（最新在前）。

        按 zoom 分档，所以「这一层」可能有多个文件；`_tier_grid_file` 取最新那个，
        而连接端点校验在没写 zoom 时必须能看出「不止一个档次」而拒绝猜。
        """
        if not valid_map_zoom(map_name, "4") or (
                tier != MAIN_GRID_KEY and not valid_grid_tier(tier)):
            return []
        if tier == MAIN_GRID_KEY:
            pattern = f"{map_name}_*.grid.npz"
            return sorted(
                (path for path in self.grid2d_dir.glob(pattern)
                 if "__" not in path.name),
                key=lambda path: path.name,
                reverse=True,
            )
        return sorted(
            self.grid2d_dir.glob(f"{map_name}__{tier}_*.grid.npz"),
            key=lambda path: path.name,
            reverse=True,
        )

    def _tier_grid_file(self, map_name: str, tier: str) -> Path | None:
        paths = self._tier_grid_candidates(map_name, tier)
        return paths[0] if paths else None

    def _cached_grid(self, path: Path) -> tuple | None:
        """按 mtime 缓存解开的 (数组, meta)；读失败返回 None。"""
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            return None
        hit = self._grid_array_cache.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
        try:
            arr, meta, _warnings = _load_grid_npz(path)
        except Exception as e:
            _log(f"读取网格失败 {path}: {e}")
            return None
        self._grid_array_cache[path] = (mtime, (arr, meta))
        while len(self._grid_array_cache) > _GRID_ARRAY_CACHE_MAX:
            self._grid_array_cache.pop(next(iter(self._grid_array_cache)))
        return arr, meta

    def _invalidate_grid_cache(self, path: Path) -> None:
        self._grid_array_cache.pop(Path(path), None)

    def _link_endpoint_grid(self, endpoint: dict,
                            query_zoom: str = "") -> tuple[Path | None, str]:
        """把连接端点解析到具体的网格文件，返回 (路径, 问题描述)。

        ``query_zoom`` 是调用方的查询上下文：端点自己没写 zoom 时（v1 老数据），
        优先按查询的 zoom 解析，只在完全没有上下文时才退化成「该层只有一个档次
        就按它算」。不能一律按唯一候选解析 —— 那样一条 zoom=3 的连接会在 zoom=4
        的查询里显示成有效。
        """
        map_name = str(endpoint.get("map") or "")
        raw_tier = str(endpoint.get("tier") or "")
        tier = raw_tier or MAIN_GRID_KEY
        zoom = str(endpoint.get("zoom") or "") or str(query_zoom or "")
        if zoom:
            path = self.grid2d_path(map_name, zoom, disk_tier(tier))
            if not path.is_file():
                return None, f"网格不存在（{path.name}）"
            return path, ""
        candidates = self._tier_grid_candidates(map_name, tier)
        if not candidates:
            return None, "网格不存在"
        if len(candidates) > 1:
            names = "、".join(path.name for path in candidates[:3])
            return None, f"该层有多个 zoom 网格（{names}…），端点必须写明 zoom"
        return candidates[0], ""

    def link_endpoint_report(self, endpoint: dict,
                             query_zoom: str = "") -> dict:
        """检查连接端点是否落在这层的可走格上。

        端点存的是世界坐标矩形，而规划器只认「从可走格走到可走格」。矩形压在
        未知格或整块落在网格外时，这条边就是通向虚空的转移 —— 编辑器里手框很容易
        画成这样（实测 7 条连接里有 3 条），所以保存前必须挡住。
        """
        report = {
            "map": str(endpoint.get("map") or ""),
            "tier": str(endpoint.get("tier") or ""),
            "zoom": str(endpoint.get("zoom") or ""),
            "grid": "",
            "rect": dict(endpoint.get("rect") or {}),
            "free": 0, "blocked": 0, "unknown": 0,
            "outside": False, "issue": "",
        }
        path, issue = self._link_endpoint_grid(endpoint, query_zoom)
        if path is None:
            report["issue"] = issue
            return report
        report["grid"] = path.name
        cached = self._cached_grid(path)
        if cached is None:
            report["issue"] = "网格读取失败"
            return report
        arr, meta = cached
        rect = endpoint.get("rect") or {}
        try:
            origin_x = float(meta["origin"][0])
            origin_z = float(meta["origin"][2])
            cell = float(meta["cell_size"])
            x0, x1 = float(rect["x0"]), float(rect["x1"])
            z0, z1 = float(rect["z0"]), float(rect["z1"])
        except (KeyError, TypeError, ValueError):
            report["issue"] = "端点或网格元数据非法"
            return report
        # 矩形覆盖到的格子下标范围（半开）。
        jx0 = math.floor((x0 - origin_x) / cell)
        jx1 = math.ceil((x1 - origin_x) / cell)
        jz0 = math.floor((z0 - origin_z) / cell)
        jz1 = math.ceil((z1 - origin_z) / cell)
        height, width = arr.shape
        cx0, cx1 = max(0, jx0), min(width, jx1)
        cz0, cz1 = max(0, jz0), min(height, jz1)
        report["outside"] = (cx1 <= cx0 or cz1 <= cz0)
        if not report["outside"]:
            sub = arr[cz0:cz1, cx0:cx1]
            report["free"] = int((sub == CELL_FREE).sum())
            report["blocked"] = int((sub == CELL_BLOCKED).sum())
            report["unknown"] = int((sub == CELL_UNKNOWN).sum())
        if report["outside"]:
            report["issue"] = "矩形整块落在这层网格的范围之外"
        elif not report["free"]:
            report["issue"] = "该区域没有可走格（Free），只有未知或阻挡格"
        return report

    @staticmethod
    def _endpoint_label(endpoint: dict) -> str:
        tier = str(endpoint.get("tier") or "")
        name = "大地图" if not tier else tier
        zoom = str(endpoint.get("zoom") or "")
        return f"{endpoint.get('map') or '?'}@{zoom or '?'}/{name}"

    @staticmethod
    def _regions_at(regions: list[dict], x: float, z: float) -> list[dict]:
        return [
            region for region in regions
            if region["x0"] <= x <= region["x1"]
            and region["z0"] <= z <= region["z1"]
        ]

    def _compute_grid_overlaps(self, map_name: str, zoom: str,
                               tier_id: str, level_id: str) -> dict:
        if not valid_map_zoom(map_name, zoom) or not valid_grid_tier(tier_id):
            return {"ok": False, "error": "非法地图名/zoom/tier"}
        current_path = self._tier_grid_file(map_name, tier_id)
        if not current_path:
            return {"ok": True, "components": [], "tier_id": tier_id}
        try:
            current, current_meta, _warnings = _load_grid_npz(current_path)
            current_regions = _normalize_floor_regions(
                current_meta.get("floor_regions") or [])
        except Exception as e:
            return {"ok": False, "error": f"读取当前网格失败: {e}"}

        wanted_level = str(level_id or "").strip()
        is_main_current = tier_id == MAIN_GRID_KEY
        layers = [
            layer for layer in self.list_layers(map_name, zoom)
            if not wanted_level
            or str(layer.get("level_id") or "") == wanted_level
        ]
        if not is_main_current and self._tier_grid_file(
                map_name, MAIN_GRID_KEY):
            layers.append({
                "id": MAIN_GRID_KEY,
                "map_name": map_name,
                "level_id": "",
                "name": "大地图",
                "group": "主地图",
            })
        layer_names = {
            str(layer.get("id") or ""): layer.get("name") or layer.get("id")
            for layer in layers
        }
        layer_names[MAIN_GRID_KEY] = "大地图"
        tier_meta = {tier_id: current_meta}
        cell_sources: dict[tuple[int, int], set[str]] = {}
        cell_y_overlap: dict[tuple[int, int], bool] = {}
        cell_matches: dict[tuple[int, int], dict[str, tuple[int, int]]] = {}
        for layer in layers:
            other_tier = str(layer.get("id") or "")
            if not other_tier or other_tier == tier_id:
                continue
            other_path = self._tier_grid_file(map_name, other_tier)
            if not other_path:
                continue
            try:
                other, other_meta, _warnings = _load_grid_npz(other_path)
                other_regions = _normalize_floor_regions(
                    other_meta.get("floor_regions") or [])
                tier_meta[other_tier] = other_meta
            except Exception as e:
                _log(f"读取重合网格失败 {other_path}: {e}")
                continue

            current_origin = [float(v) for v in current_meta["origin"]]
            other_origin = [float(v) for v in other_meta["origin"]]
            current_size = float(current_meta["cell_size"])
            other_size = float(other_meta["cell_size"])
            aligned = (
                current.shape == other.shape
                and math.isclose(current_origin[0], other_origin[0], abs_tol=1e-6)
                and math.isclose(current_origin[2], other_origin[2], abs_tol=1e-6)
                and math.isclose(current_size, other_size, rel_tol=1e-6)
            )
            if aligned:
                rows, cols = np.nonzero((current != CELL_UNKNOWN)
                                        & (other != CELL_UNKNOWN))
            else:
                rows, cols = np.nonzero(current)
            for iz, ix in zip(rows.tolist(), cols.tolist()):
                wx = current_origin[0] + (ix + 0.5) * current_size
                wz = current_origin[2] + (iz + 0.5) * current_size
                if aligned:
                    jx, jz = int(ix), int(iz)
                else:
                    jx = math.floor((wx - other_origin[0]) / other_size)
                    jz = math.floor((wz - other_origin[2]) / other_size)
                    if (jx < 0 or jz < 0 or
                            jz >= other.shape[0] or jx >= other.shape[1]):
                        continue
                    if int(other[jz, jx]) == CELL_UNKNOWN:
                        continue
                key = (int(ix), int(iz))
                mine = self._regions_at(current_regions, wx, wz)
                theirs = self._regions_at(other_regions, wx, wz)
                y_overlap = any(
                    a["y_min"] <= b["y_max"] and b["y_min"] <= a["y_max"]
                    for a in mine for b in theirs
                )
                cell_sources.setdefault(key, set()).add(other_tier)
                cell_matches.setdefault(key, {})[other_tier] = (jx, jz)
                if y_overlap:
                    cell_y_overlap[key] = True

        group_of = {
            key: (frozenset(sources), bool(cell_y_overlap.get(key)))
            for key, sources in cell_sources.items()
        }
        unvisited = set(cell_sources)
        components = []
        directions = [
            (1, 0), (-1, 0), (0, 1), (0, -1),
            (1, 1), (1, -1), (-1, 1), (-1, -1),
        ]
        while unvisited:
            start = next(iter(unvisited))
            group = group_of[start]
            stack = [start]
            unvisited.remove(start)
            cells = []
            while stack:
                key = stack.pop()
                cells.append(key)
                ix, iz = key
                for dx, dz in directions:
                    neighbor = (ix + dx, iz + dz)
                    if neighbor not in unvisited or group_of.get(neighbor) != group:
                        continue
                    unvisited.remove(neighbor)
                    stack.append(neighbor)
            cells.sort()
            severity = "three_d" if group[1] else "xz_only"
            component_layers = [
                tier_id, *sorted(group[0])
            ]
            digest = hashlib.sha1(
                (tier_id + "|" + ",".join(
                    f"{ix}:{iz}" for ix, iz in cells)).encode("utf-8")
            ).hexdigest()[:16]
            x0 = min(ix for ix, _iz in cells)
            x1 = max(ix for ix, _iz in cells) + 1
            z0 = min(iz for _ix, iz in cells)
            z1 = max(iz for _ix, iz in cells) + 1
            rects_by_tier: dict[str, list[list[float]]] = {tier_id: []}
            for ix, iz in cells:
                wx0 = current_origin[0] + ix * current_size
                wx1 = current_origin[0] + (ix + 1) * current_size
                wz0 = current_origin[2] + iz * current_size
                wz1 = current_origin[2] + (iz + 1) * current_size
                rects_by_tier[tier_id].append([wx0, wx1, wz0, wz1])
            for source_tier in group[0]:
                source_meta = tier_meta.get(source_tier)
                if not source_meta:
                    continue
                source_origin = [float(value) for value in source_meta["origin"]]
                source_size = float(source_meta["cell_size"])
                rects = []
                for ix, iz in cells:
                    match = cell_matches.get((ix, iz), {}).get(source_tier)
                    if not match:
                        continue
                    jx, jz = match
                    cx0 = current_origin[0] + ix * current_size
                    cx1 = current_origin[0] + (ix + 1) * current_size
                    cz0 = current_origin[2] + iz * current_size
                    cz1 = current_origin[2] + (iz + 1) * current_size
                    ox0 = source_origin[0] + jx * source_size
                    ox1 = source_origin[0] + (jx + 1) * source_size
                    oz0 = source_origin[2] + jz * source_size
                    oz1 = source_origin[2] + (jz + 1) * source_size
                    rect = [
                        max(cx0, ox0), min(cx1, ox1),
                        max(cz0, oz0), min(cz1, oz1),
                    ]
                    if rect[0] < rect[1] and rect[2] < rect[3]:
                        rects.append(rect)
                rects_by_tier[source_tier] = rects
            current_rects = rects_by_tier[tier_id]
            components.append({
                "id": digest,
                "severity": severity,
                "cells": [[ix, iz] for ix, iz in cells],
                "bounds": {"x0": x0, "x1": x1, "z0": z0, "z1": z1},
                "world_bounds": {
                    "x0": min(rect[0] for rect in current_rects),
                    "x1": max(rect[1] for rect in current_rects),
                    "z0": min(rect[2] for rect in current_rects),
                    "z1": max(rect[3] for rect in current_rects),
                },
                "layers": component_layers,
                "layer_names": [
                    layer_names.get(value, value) for value in component_layers
                ],
                "_rects_by_tier": rects_by_tier,
                "_cell_matches": {
                    f"{ix}:{iz}": cell_matches.get((ix, iz), {})
                    for ix, iz in cells
                },
            })
        return {
            "ok": True,
            "tier_id": tier_id,
            "components": components,
        }

    def get_grid_overlaps(self, map_name: str, zoom: str,
                          tier_id: str, level_id: str) -> dict:
        """Find connected overlap components shared by grid tiers."""
        result = self._compute_grid_overlaps(
            map_name, zoom, tier_id, level_id)
        if not result.get("ok"):
            return result
        components = []
        xz_only = []
        three_d = []
        for component in result["components"]:
            public = {
                key: value for key, value in component.items()
                if not key.startswith("_")
            }
            components.append(public)
            cells = component["cells"]
            if component["severity"] == "three_d":
                three_d.extend(cells)
            else:
                xz_only.extend(cells)
        return {
            "ok": True,
            "tier_id": tier_id,
            "components": components,
            "xz_only": xz_only,
            "three_d": three_d,
        }

    def save_overlap_component_height(
            self, map_name: str, zoom: str, tier_id: str, level_id: str,
            component_id: str, assignments: list[dict],
            replace_component_ids: dict | None = None) -> dict:
        """Define height intervals for one connected overlap component."""
        result = self._compute_grid_overlaps(
            map_name, zoom, tier_id, level_id)
        if not result.get("ok"):
            return result
        component = next(
            (item for item in result["components"]
             if item["id"] == str(component_id or "")),
            None,
        )
        if not component:
            return {"ok": False, "error": "找不到指定的重合连通区"}
        if not isinstance(assignments, list) or not assignments:
            return {"ok": False, "error": "assignments 必须是非空数组"}

        normalized = []
        for index, item in enumerate(assignments):
            if not isinstance(item, dict):
                return {"ok": False, "error": f"assignments[{index}] 必须是对象"}
            target_tier = str(item.get("tier_id") or "").strip()
            if target_tier not in component["layers"]:
                return {"ok": False,
                        "error": f"层级 {target_tier} 不属于该重合连通区"}
            try:
                y_min = float(item["y_min"])
                y_max = float(item["y_max"])
            except (KeyError, TypeError, ValueError, OverflowError):
                return {"ok": False,
                        "error": f"assignments[{index}] 需要 y_min/y_max"}
            if not (math.isfinite(y_min) and math.isfinite(y_max)):
                return {"ok": False, "error": "高度必须是有限数"}
            if y_min > y_max:
                return {"ok": False, "error": "y_min 不能大于 y_max"}
            normalized.append((target_tier, y_min, y_max))

        saved = []
        replace_map: dict[str, set[str]] = {}
        if isinstance(replace_component_ids, dict):
            for key, values in replace_component_ids.items():
                if not isinstance(values, list):
                    continue
                replace_map[str(key)] = {
                    str(value) for value in values if str(value or "")
                }
        for target_tier, y_min, y_max in normalized:
            path = self._tier_grid_file(map_name, target_tier)
            if not path:
                return {"ok": False, "error": f"层级网格不存在: {target_tier}"}
            rects = component["_rects_by_tier"].get(target_tier) or []
            if not rects:
                return {"ok": False,
                        "error": f"层级没有可用的重合格子: {target_tier}"}
            x0 = min(rect[0] for rect in rects)
            x1 = max(rect[1] for rect in rects)
            z0 = min(rect[2] for rect in rects)
            z1 = max(rect[3] for rect in rects)
            try:
                meta = _load_grid_meta_npz(path)
                regions = _normalize_floor_regions(
                    meta.get("floor_regions") or [])
            except Exception as e:
                return {"ok": False,
                        "error": f"读取层级网格失败 {target_tier}: {e}"}
            regions = [
                region for region in regions
                if region.get("component_id") != component["id"]
                and region.get("component_id")
                    not in replace_map.get(target_tier, set())
                and _region_overlap_ratio(region, rects) < 0.8
            ]
            regions.append({
                "component_id": component["id"],
                "x0": x0, "x1": x1, "z0": z0, "z1": z1,
                "cells": rects,
                "y_min": y_min, "y_max": y_max,
            })
            write = self.save_grid_floor_regions(
                map_name, zoom, disk_tier(target_tier), regions)
            if not write.get("ok"):
                return write
            saved.append({
                "tier_id": target_tier,
                "component_id": component["id"],
                "y_min": y_min,
                "y_max": y_max,
                "cell_count": len(rects),
            })
        return {"ok": True, "component_id": component["id"], "saved": saved}

    def _on_position(self, data: dict) -> None:
        """官方 WS 收到位置 → 用坐标判断楼层 → 广播 coords 事件。

        判定**不看 levelId**（官方那个字段不可信），只用 x/y/z 落在哪一层的高度
        区间里，见 :meth:`resolve_tier_by_height`。
        """
        enriched = dict(data) if isinstance(data, dict) else {"value": data}
        pos = enriched.get("pos") if isinstance(enriched.get("pos"), dict) else enriched
        map_name = str(enriched.get("mapId") or "").strip()
        if map_name and pos:
            resolution = self.resolve_tier_by_height(
                map_name, pos.get("x"), pos.get("y"), pos.get("z"))
            enriched["gridTierId"] = resolution["tier_id"]
            enriched["gridFloorStatus"] = resolution["status"]
            enriched["gridFloorReason"] = resolution["reason"]
            enriched["gridFloorCandidates"] = resolution["matched"]
        self.bus.emit(type="coords", data=enriched)

    def _on_position_state(self, state: dict) -> None:
        self.bus.emit(type="position_state", state=state)

    def _position_log(self, msg: str) -> None:
        _log(f"[position] {msg}")
        self.bus.emit(type="log", text=msg)

    def relay_coords(self, raw_body: bytes) -> dict:
        """POST /api/coords：把游戏/外部客户端上报的坐标广播给所有页面"""
        try:
            data = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except Exception as e:
            return {"ok": False, "error": f"JSON 解析失败: {e}"}
        # 兼容 {"data": {"pos": ...}} 与 {"pos": ...} 两种格式，拍平成一层
        if isinstance(data, dict) and "data" in data:
            data = data["data"]
        self._on_position(data)
        return {"ok": True, "clients": self.bus.subscriber_count}

    # ---------- 官方分层地图 ----------
    def _layer_entries(self) -> list[dict]:
        path = self.store.tiles_root / "layers" / CATALOG_FILENAME
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            return builtin_layer_entries()
        if self._layer_catalog_cache and self._layer_catalog_cache[0] == mtime:
            return self._layer_catalog_cache[1]
        try:
            entries = load_layer_catalog(path)
        except Exception as e:
            _log(f"读取分层目录失败 {path}: {e}")
            return builtin_layer_entries()
        self._layer_catalog_cache = (mtime, entries)
        return entries

    def list_layers(self, map_name: str, zoom: str) -> list[dict]:
        """列出当前底图可用的官方分层图片。"""
        return list_layer_catalog(map_name, zoom, self._layer_entries())

    def get_layer_image(self, map_name: str, zoom: str, layer_id: str) -> bytes | None:
        """读取官方分层图片；首次访问时缓存到 tiles/layers/。"""
        entry = find_layer(map_name, zoom, layer_id, self._layer_entries())
        if not entry:
            return None
        dest = self.store.tiles_root / layer_cache_relpath(entry)
        if dest.is_file():
            try:
                return dest.read_bytes()
            except OSError as e:
                _log(f"读取分层图片失败 {dest}: {e}")
                return None

        try:
            data = _download_layer_image(layer_source_url(entry))
        except Exception as e:
            _log(f"下载分层图片失败 {entry['filename']}: {e}")
            return None

        if Image is not None:
            try:
                with Image.open(BytesIO(data)) as img:
                    img.verify()
            except Exception as e:
                _log(f"分层图片校验失败 {entry['filename']}: {e}")
                return None

        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = _unique_tmp(dest)
            with _path_lock(dest):
                try:
                    tmp.write_bytes(data)
                    _replace_with_retry(tmp, dest)
                except Exception:
                    tmp.unlink(missing_ok=True)
                    raise
        except OSError as e:
            _log(f"写入分层图片缓存失败 {dest}: {e}")
            return None
        return data

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

    def grid2d_path(self, map_name: str, zoom: str, tier: str = "") -> Path:
        """2D 网格的落盘位置。

        单一位置，不再有"读哪写哪"的旧位置兼容分支：新格式文件名是
        <地图>_<zoom>.grid.npz（没有 _2d 中缀）。
        """
        # 两种口径都收（客户端可能直接送 __main__），落盘一律用空串口径
        tier = disk_tier(tier)
        if tier:
            return self.grid2d_dir / f"{map_name}__{tier}_{zoom}{GRID_SUFFIX}"
        return self.grid2d_dir / f"{map_name}_{zoom}{GRID_SUFFIX}"

    def get_grid2d(self, map_name: str, zoom: str, tier: str = "") -> dict:
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
        if not valid_map_zoom(map_name, zoom) or not valid_grid_tier(tier):
            return {"data": None, "source": None, "error": "非法地图名/zoom"}
        p = self.grid2d_path(map_name, zoom, tier)
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
                "tier": str(meta.get("tier") or ""),
                "cells": cells,
                "blocked": blocked,
                "floor_regions": list(meta.get("floor_regions") or []),
            },
            "source": str(p),
            "warnings": warnings,
        }

    # ---------- 跨层连接（网格 via） ----------
    def get_grid_links(self, map_name: str = "", zoom: str = "") -> dict:
        """读连接，并逐条附上端点校验结果。

        校验是只读的、不改数据：文件里可能已经躺着端点悬空的连接（实测有过），
        直接拒读或者静默丢掉都会让问题看不见，所以标出来交给编辑器和用户处理。
        """
        path = links_path(self.grid2d_dir)
        try:
            data = load_links(path)
        except Exception as e:
            _log(f"读取分层连接失败 {path}: {e}")
            return {"schema_version": LINK_SCHEMA_VERSION, "links": [],
                    "error": str(e)}
        wanted_zoom = str(zoom or "")
        links = []
        for link in data["links"]:
            if not isinstance(link, dict):
                continue
            if not self._link_matches(link, map_name, wanted_zoom):
                continue
            checks = {}
            for side in ("from", "to"):
                endpoint = link.get(side)
                checks[side] = (
                    self.link_endpoint_report(endpoint, wanted_zoom)
                    if isinstance(endpoint, dict)
                    else {"issue": "端点缺失"}
                )
            problems = [
                f"{'起点' if side == 'from' else '终点'}：{checks[side]['issue']}"
                for side in ("from", "to") if checks[side].get("issue")
            ]
            links.append({**link, "checks": {
                "from": checks["from"], "to": checks["to"],
                "valid": not problems, "issue": "；".join(problems),
            }})
        return {**data, "links": links}

    @staticmethod
    def _link_matches(link: dict, map_name: str, zoom: str) -> bool:
        """连接是否属于某个 地图/zoom —— 任一端命中即可。

        端点没写 zoom 时（v1 老数据）算命中：它会按查询的 zoom 解析，只要真的
        不属于这一档，端点校验就会报出来，不该在这里被静默过滤掉。
        """
        if not map_name and not zoom:
            return True
        for side in ("from", "to"):
            endpoint = link.get(side)
            if not isinstance(endpoint, dict):
                continue
            if map_name and str(endpoint.get("map") or "") != map_name:
                continue
            endpoint_zoom = str(endpoint.get("zoom") or "")
            if zoom and endpoint_zoom and endpoint_zoom != zoom:
                continue
            return True
        return False

    def save_grid_link(self, raw: dict) -> dict:
        """保存连接：两个端点都必须落在这层的可走格上，否则拒收。"""
        try:
            link = normalize_link(raw, str(raw.get("id") or ""))
        except Exception as e:
            return {"ok": False, "error": str(e)}
        problems = []
        for side, label in (("from", "起点"), ("to", "终点")):
            report = self.link_endpoint_report(link[side])
            if report["issue"]:
                problems.append(
                    f"{label} {self._endpoint_label(link[side])}：{report['issue']}")
        if problems:
            return {"ok": False,
                    "error": "连接端点无效，已拒绝保存（" + "；".join(problems) + "）"}
        try:
            saved = upsert_link(links_path(self.grid2d_dir), link)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "link": saved}

    def delete_grid_link(self, link_id: str) -> dict:
        try:
            changed = delete_link(links_path(self.grid2d_dir), link_id)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "deleted": changed}

    # ---------- 分层导航图：把「能不能导航」变成可判定的东西 ----------
    #
    # 规则（消费方必须照这个来，编辑器这边负责产出的数据满足它）：
    #   1. 相邻关系只在**同一层内**成立（每层各自的稠密网格）；
    #   2. 跨层一律走 links.json 的显式边，任何「把两层按 X/Z 叠在一起」的做法
    #      都是错的 —— 同一点在 X/Z 上同时属于地面和地下，叠起来等于凭空连边；
    #   3. 主地图**不挖洞**：地表那一格是真实可走的，地下房间在它下面。把主图
    #      挖成 Blocked 会删掉真实可走的表面。位置属于哪一层由 floor_regions 的
    #      Y 区间判定（见 resolve_tier_by_height），不靠挖格子。
    # 于是「可导航」= 端点都有效 + 每处与主图/其它层重合的区域都标了高度区间。
    def nav_graph(self, map_name: str, zoom: str, summary: bool = False) -> dict:
        """汇总一层地图的导航输入，并给出就绪判定。

        ``summary=True`` 时不给逐格坐标、只给 ``free_count``：界面只想显示「能不能
        导航」时没必要为此传几万个坐标（map02@4 约 4.3 万格）。
        """
        if not valid_map_zoom(map_name, zoom):
            return {"ok": False, "error": "非法地图名/zoom"}

        tiers = []
        for tier_id in self._grid_tier_ids(map_name, zoom):
            tier = disk_tier(tier_id)
            path = self.grid2d_path(map_name, zoom, tier)
            if not path.is_file():
                continue
            cached = self._cached_grid(path)
            if cached is None:
                continue
            arr, meta = cached
            try:
                regions = _normalize_floor_regions(meta.get("floor_regions") or [])
            except ValueError as e:
                _log(f"网格楼层区间非法 {path.name}: {e}")
                regions = []
            rows, cols = np.nonzero(arr == CELL_FREE)
            entry = {
                "tier_id": tier or MAIN_GRID_KEY,
                "grid": path.name,
                "origin": [float(meta["origin"][0]), float(meta["origin"][2])],
                "cell_size": float(meta["cell_size"]),
                "shape": [int(arr.shape[0]), int(arr.shape[1])],
                "floor_regions": regions,
            }
            if summary:
                entry["free_count"] = int(rows.size)
            else:
                entry["free_cells"] = [[int(c), int(r)] for r, c in zip(rows, cols)]
            tiers.append(entry)

        if not tiers:
            return {"ok": False, "error": "该地图/zoom 下没有任何网格文件"}

        blockers = []
        links = self.get_grid_links(map_name, zoom)["links"]
        for link in links:
            if not link["checks"]["valid"]:
                blockers.append(f"连接 {link['id']} 端点无效：{link['checks']['issue']}")
        # 每个有网格的层各算一次重合（连通区的下标是「当前层」的口径，换层要重算）。
        # 同一处重合区会从多个视角报出来，格数和层名顺序都随视角变；但各层实际的
        # 世界矩形是一致的，所以按「涉及层 + 缺哪层 + 世界包围盒」归并，取最大格数。
        grouped: dict[tuple, list] = {}
        for tier in self._grid_tier_ids(map_name, zoom):
            result = self._compute_grid_overlaps(map_name, zoom, tier, "")
            if not result.get("ok"):
                continue
            for component in result["components"]:
                if component["severity"] == "three_d":
                    continue
                missing = []
                for tier_id, name in zip(
                        component["layers"], component["layer_names"]):
                    regions = self._tier_regions(
                        self._nav_tier_meta(map_name, zoom, tier_id))
                    rects = component["_rects_by_tier"].get(tier_id) or []
                    if not rects or not self._regions_cover_rects(regions, rects):
                        missing.append(name)
                if not missing:
                    continue
                key = (
                    frozenset(component["layers"]),
                    frozenset(missing),
                    self._component_world_key(component),
                )
                entry = grouped.setdefault(
                    key, [0, list(component["layer_names"]), list(missing)])
                entry[0] = max(entry[0], len(component["cells"]))
        for count, names, missing in grouped.values():
            layers_text = " / ".join(self._sort_layer_names(names))
            missing_text = " / ".join(self._sort_layer_names(missing))
            blockers.append(
                f"重合区 {count} 格（{layers_text}）在 X/Z 上重叠，"
                f"{missing_text} 还没标高度区间 —— 导航会在这些格子上串楼层"
            )
        return {
            "ok": True,
            "map": map_name,
            "zoom": zoom,
            "ready": not blockers,
            "blockers": blockers,
            "tiers": tiers,
            "links": links,
            # 跨层边只可能来自 links —— 这里刻意不生成任何隐式跨层邻接。
            "cross_tier_edges_from": "links",
        }

    @staticmethod
    def _component_world_key(component: dict) -> tuple:
        """重合区的世界包围盒（跨视角稳定的那一份身份标识）。

        连通区的下标是「当前层」的口径，换层就变；但每层实际相交出来的世界矩形
        是一样的，取全体矩形的并集做包围盒，就能把同一处重合区从不同视角对上。
        """
        rects = [
            rect
            for group in (component.get("_rects_by_tier") or {}).values()
            for rect in group
        ]
        if not rects:
            return ()
        return (
            round(min(rect[0] for rect in rects), 3),
            round(min(rect[2] for rect in rects), 3),
            round(max(rect[1] for rect in rects), 3),
            round(max(rect[3] for rect in rects), 3),
        )

    @staticmethod
    def _sort_layer_names(names) -> list[str]:
        """层名排序：大地图优先，其余按名字，让同一处问题的措辞稳定下来。"""
        unique = sorted(set(names), key=lambda text: (text != "大地图", text))
        return unique

    def _grid_tier_ids(self, map_name: str, zoom: str) -> list[str]:
        """该地图在给定 zoom 下**确实有网格文件**的层，主地图用 __main__ 表示。

        按文件枚举而不是按分层目录：目录里有几十层，没画过的层去算重合纯属浪费。
        """
        if not valid_map_zoom(map_name, zoom):
            return []
        ids = []
        if self.grid2d_path(map_name, zoom, "").is_file():
            ids.append(MAIN_GRID_KEY)
        prefix = f"{map_name}__"
        suffix = f"_{zoom}{GRID_SUFFIX}"
        for path in sorted(self.grid2d_dir.glob(f"{map_name}__*{GRID_SUFFIX}")):
            name = path.name
            if not name.startswith(prefix) or not name.endswith(suffix):
                continue
            tier = name[len(prefix):-len(suffix)]
            if tier and valid_grid_tier(tier):
                ids.append(tier)
        return ids

    def _nav_tier_meta(self, map_name: str, zoom: str, tier_id: str) -> dict | None:
        path = self.grid2d_path(map_name, zoom, disk_tier(tier_id))
        cached = self._cached_grid(path) if path.is_file() else None
        return cached[1] if cached else None

    @staticmethod
    def _tier_regions(meta: dict | None) -> list[dict]:
        if not meta:
            return []
        try:
            return _normalize_floor_regions(meta.get("floor_regions") or [])
        except ValueError:
            return []

    @staticmethod
    def _regions_cover_rects(regions: list[dict], rects: list[list[float]]) -> bool:
        """这层的高度区间是否盖住了这些世界矩形。

        连通区的下标是「当前层」的口径，跨层换算容易出错；重叠分析已经把每层
        各自的矩形算好了（``_rects_by_tier``），所以这里只比世界坐标。
        """
        if not regions or not rects:
            return False
        for rect in rects:
            cx = (float(rect[0]) + float(rect[1])) / 2.0
            cz = (float(rect[2]) + float(rect[3])) / 2.0
            hit = False
            for region in regions:
                if not (region["x0"] <= cx <= region["x1"]
                        and region["z0"] <= cz <= region["z1"]):
                    continue
                cells = region.get("cells")
                if cells:
                    hit = any(cell[0] <= cx <= cell[1]
                              and cell[2] <= cz <= cell[3] for cell in cells)
                else:
                    hit = True
                if hit:
                    break
            if not hit:
                return False
        return True

    def save_grid2d(self, map_name: str, zoom: str, data: dict,
                    tier: str = "") -> dict:
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
        if not valid_map_zoom(map_name, zoom) or not valid_grid_tier(tier):
            return {"ok": False, "error": "非法地图名/zoom"}
        if "source_tier" in data:
            source_tier = str(data.get("source_tier") or "")
            if source_tier != str(tier or ""):
                return {
                    "ok": False,
                    "error": "网格来源层级不匹配：拒绝用 "
                             f"{source_tier or '__main__'} 的文档覆盖 "
                             f"{tier or '__main__'}",
                }
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
        try:
            floor_regions = _normalize_floor_regions(
                data.get("floor_regions", []))
        except ValueError as e:
            return {"ok": False, "error": str(e)}

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
            "tier": tier,
            # 平移成 cells[0,0] 最小角的世界坐标
            "origin": [origin[0] + min_ix * cell_size, 0.0, origin[2] + min_iz * cell_size],
            "cell_size": cell_size,
            "axis_convention": GRID_AXIS_CONVENTION,
            "floor_regions": floor_regions,
            "source": "nav-grid-editor",
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        out = self.grid2d_path(map_name, zoom, tier)
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
        self._invalidate_grid_cache(out)
        # 顺带把层声明索引物化到数据目录：新增的层要能被消费侧枚举到。尽力而为 ——
        # 索引写不进去不该让网格保存失败。
        try:
            self.sync_level_index()
        except Exception as e:  # noqa: BLE001
            _log(f"层声明索引刷新失败: {e}")
        return {"ok": True, "saved": out.name, "path": str(out),
                "dir": str(out.parent), "overwrote": existed,
                "shape": [height, width],
                "origin": list(meta["origin"]),
                "floor_regions": list(meta["floor_regions"]),
                # 服务端按"原范围 ∪ 已涂格子"重定了下标基准，前端要把内存里的
                # 格子坐标回移这么多，才能保持"下标 0 ⇔ origin"这一不变量
                "shift": [min_ix, min_iz]}

    def save_grid_floor_regions(self, map_name: str, zoom: str, tier: str,
                                raw_regions) -> dict:
        """Update only one grid file's floor_regions metadata."""
        # 主地图在 API 口径下叫 __main__，落盘要用无 tier 的文件名
        tier = disk_tier(tier)
        if not valid_map_zoom(map_name, zoom) or (
                tier and not valid_grid_tier(tier)):
            return {"ok": False, "error": "非法地图名/zoom/tier"}
        try:
            floor_regions = _normalize_floor_regions(raw_regions)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        path = self.grid2d_path(map_name, zoom, tier)
        if not path.is_file():
            return {"ok": False, "error": "网格文件不存在"}
        tmp = _unique_tmp(path)
        with _path_lock(path):
            try:
                arr, meta, _warnings = _load_grid_npz(path)
                meta["floor_regions"] = floor_regions
                with open(tmp, "wb") as fh:
                    np.savez_compressed(
                        fh,
                        cells=arr,
                        meta=np.array(json.dumps(meta, ensure_ascii=False)),
                    )
                _load_grid_npz(tmp)
                _replace_with_retry(tmp, path)
            except Exception as e:
                tmp.unlink(missing_ok=True)
                return {"ok": False, "error": f"保存失败（未覆盖原文件）: {e}"}
        self._floor_grid_cache.pop(path, None)
        self._invalidate_grid_cache(path)
        return {"ok": True, "path": str(path),
                "floor_regions": floor_regions}

# =========================================================
# 2D 网格读取：与 ok-end-field 的读方同级校验
# =========================================================
