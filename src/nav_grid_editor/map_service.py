#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""地图瓦片抓取 / 合成 Web 服务（独立工程，数据均在本项目目录内）。

功能移植自 F:\\QQBot\\python\\wsserver，但已完全独立：
- 瓦片抓取（Playwright，思路同原 map/map_tile_downloader.py）：
    打开游戏地图页面，拦截瓦片请求逐张落盘到 tiles/run_<时间戳>/... 会话，
    只抓取未下载过的瓦片，结束时自动合并到 tiles/latest；
    登录配置保存在本项目 browser_profile/ 下。
- 合成（思路同原 map/map_tile_stitcher.py + tools/merge_tiles.py）：
    把某个地图/zoom 的瓦片流式推送给浏览器，实时看到逐张贴上总图；
    装有 Pillow 时还会保存拼接总图到 tiles/maps/<地图>/<zoom>/（只保留一份）。
- 模拟抓取（不落盘）：纯内存生成瓦片流，用于无登录态时演示实时贴图效果。
- SSE 事件总线：tile / stats / log / coords / hello 等事件广播给所有浏览器页面。

所有依赖均为可选：没有 playwright 则抓取不可用，没有 Pillow 则 WebP 转 PNG 与
总图落盘不可用，其余功能不受影响。例外是 numpy —— 2D 网格的稠密 npz 读写依赖它。
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import struct
import sys
import threading
import time
import zlib
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import numpy as np  # 2D 网格的稠密 npz 读写必需（非可选依赖）

# =========================================================
# 可选依赖
# =========================================================

try:
    from PIL import Image  # type: ignore
except Exception:  # pragma: no cover
    Image = None

PLAYWRIGHT_OK = False
try:
    import playwright  # noqa: F401
    PLAYWRIGHT_OK = True
except Exception:  # pragma: no cover
    PLAYWRIGHT_OK = False

# =========================================================
# 配置
# =========================================================

MAP_URL = "https://game.skland.com/map/endfield"

# ---- 2D 网格稠密 npz 的格式常量 ----
# 必须与 ok-end-field/src/nav/grid_io.py 逐字一致（跨仓库不能 import，只能字面量对齐）：
# 读方会校验 magic/schema_version/axis_convention，值域只接受下面这三种状态。
GRID_MAGIC = "okef-nav-grid"
GRID_SCHEMA_VERSION = 2
GRID_SUFFIX = ".grid.npz"
GRID_AXIS_CONVENTION = (
    "world = origin + (i,j)*cell_size; i=row=z, j=col=x, both positive; "
    "origin is the min corner of cells[0,0]; 2D ignores y"
)
CELL_UNKNOWN = 0
CELL_FREE = 1
CELL_BLOCKED = 2
#: 稠密数组元素数上限：格子放得太散时 (max-min)² 会直接吃光内存，超限就拒绝
DENSE_CELL_LIMIT = 4_000_000
#: 编辑器可保存的已涂格子上限（与前端框选上限一致）
EDIT_CELL_LIMIT = 300_000
#: 与 ok-end-field 的 grid_io.load_grid / scripts/nav/verify_grid.py 对齐的硬性约束
GRID_MEMBERS = ("cells", "meta")
GRID_STATES = frozenset((CELL_UNKNOWN, CELL_FREE, CELL_BLOCKED))

# 独立工程：所有数据都放在本项目目录内，不依赖外部路径。
# 全部数据都在 nav-grid-editor 内：
#   tiles/                     瓦片数据（latest / run_* 会话 / maps 总图）
#   browser_profile/           Playwright 持久化登录配置

# 数据目录（tiles/ grids2d/ browser_profile/ 的父目录）
def default_data_root() -> Path:
    """数据根目录。

    默认取**当前工作目录**：tiles/ grids2d/ 这些是项目数据，跟着"你在哪运行"
    走，而不是跟着代码包走。以前默认取 ``__file__`` 所在目录，代码搬进 src/
    之后会指到包内部，所以统一改成 cwd + 显式覆盖
    （``--data-root`` / ``NAV_DATA_ROOT``）。
    """
    env = os.environ.get("NAV_DATA_ROOT", "")
    if env:
        return Path(env)
    return Path.cwd()


# 瓦片根目录（可用环境变量 NAV_TILES_ROOT 覆盖）
def default_tiles_root(root: Path | None = None) -> Path:
    env = os.environ.get("NAV_TILES_ROOT", "")
    if env:
        return Path(env)
    return (Path(root) if root else default_data_root()) / "tiles"


# 持久化浏览器配置（登录态）目录（可用环境变量 NAV_PROFILE_DIR 覆盖）
def default_profile_dir(root: Path | None = None) -> Path:
    env = os.environ.get("NAV_PROFILE_DIR", "")
    if env:
        return Path(env)
    return (Path(root) if root else default_data_root()) / "browser_profile"


TILE_SIZE = 256
RUN_PREFIX = "run"
DEBUG = bool(os.environ.get("NAV_DEBUG", ""))

# 瓦片 URL 格式: /tile(map02_1 之类)/<map>/<zoom>/<x>_<y>.png
tile_pattern = re.compile(
    r"/tile(?:_[^/]+)?/([^/]+)/(\d+)/(-?\d+)_(-?\d+)\.png"
)
# 瓦片文件名: x_y.png
TILE_FILE_PATTERN = re.compile(r"^(-?\d+)_(-?\d+)\.png$")

# 模拟抓取参数
SIM_MAP = "sim"
SIM_ZOOM = "4"
SIM_COLS, SIM_ROWS = 10, 8
SIM_DELAY = 0.04  # 每张瓦片间隔（秒），让贴图过程肉眼可见

# =========================================================
# 小工具：无 Pillow 依赖的纯色 PNG 生成（模拟抓取用）
# =========================================================

def solid_png(w: int, h: int, rgb: tuple) -> bytes:
    def chunk(typ: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + typ + data
            + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)
        )
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8bit RGB
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))  # 每行 filter=0
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def _log(msg: str):
    print(f"[map] {msg}", flush=True)


# =========================================================
# 事件总线（SSE 广播）
# =========================================================

class EventBus:
    def __init__(self):
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=4000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            try:
                self._subs.remove(q)
            except ValueError:
                pass

    def emit_obj(self, event: dict):
        """广播一个事件（自动 JSON 化）"""
        text = json.dumps(event, ensure_ascii=False)
        with self._lock:
            for q in self._subs:
                try:
                    q.put_nowait(text)
                except queue.Full:
                    try:  # 慢客户端：丢最旧的一条
                        q.get_nowait()
                        q.put_nowait(text)
                    except Exception:
                        pass

    def emit(self, **kw):
        self.emit_obj(kw)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)


# =========================================================
# 瓦片存储（读取 / 解析，与 wsserver 目录结构兼容）
# =========================================================

class TileStore:
    def __init__(self, tiles_root: Path):
        self.tiles_root = Path(tiles_root)
        self.active_session_dir: Path | None = None  # 抓取中正在写入的会话

    # ---------- 目录解析 ----------
    def _sessions(self) -> list[Path]:
        if not self.tiles_root.is_dir():
            return []
        return sorted(
            p for p in self.tiles_root.iterdir()
            if p.is_dir() and p.name.startswith(f"{RUN_PREFIX}_")
        )

    def _compose_candidates(self, map_name: str, zoom: str) -> list[Path]:
        """按优先级返回可能存有该地图/zoom 瓦片的目录"""
        cands: list[Path] = []
        # 1. 抓取中的会话（最新写入优先）
        if self.active_session_dir:
            cands.append(self.active_session_dir / map_name / zoom)
        # 2. 汇总目录
        cands.append(self.tiles_root / "latest" / map_name / zoom)
        # 3. 已结束的会话（时间从新到旧）
        for s in reversed(self._sessions()):
            if self.active_session_dir and s == self.active_session_dir:
                continue
            cands.append(s / map_name / zoom)
        # 4. 旧版扁平目录
        cands.append(self.tiles_root / map_name / zoom)
        return cands

    def resolve_dir(self, map_name: str, zoom: str) -> Path | None:
        """**第一个**存在瓦片的候选目录（不合并）。

        只用于判断"这张图有没有瓦片"和打印路径，**不要**拿它当瓦片全集：
        抓取会话目录只是本次新增的增量，完整集合要用 :meth:`iter_tiles`。
        """
        for d in self._compose_candidates(map_name, zoom):
            if d.is_dir():
                return d
        return None

    def tile_bytes(self, map_name: str, zoom: str, x: int, y: int) -> bytes | None:
        for d in self._compose_candidates(map_name, zoom):
            f = d / f"{x}_{y}.png"
            if f.is_file():
                try:
                    return f.read_bytes()
                except OSError as e:
                    _log(f"读取瓦片失败 {f}: {e}")
                    return None
        return None

    # ---------- 查询 ----------
    def list_maps(self) -> dict[str, list[str]]:
        """返回 {地图名: [zoom...]}，来源：latest + 会话 + 旧目录"""
        result: dict[str, set[str]] = {}
        if not self.tiles_root.is_dir():
            return {k: sorted(v) for k, v in result.items()}

        def scan(d: Path):
            if not d.is_dir():
                return
            for map_dir in sorted(d.iterdir()):
                if not map_dir.is_dir():
                    continue
                for zoom_dir in map_dir.iterdir():
                    if zoom_dir.is_dir():
                        result.setdefault(map_dir.name, set()).add(zoom_dir.name)

        scan(self.tiles_root / "latest")
        for s in self._sessions():
            scan(s)
        # 旧版扁平目录（跳过 latest/maps/marks/icons/run_*）
        for d in sorted(self.tiles_root.iterdir()):
            if not d.is_dir() or d.name in ("latest", "maps", "marks", "icons"):
                continue
            if d.name.startswith(f"{RUN_PREFIX}_"):
                continue
            scan(d)
        return {k: sorted(v) for k, v in result.items()}

    def iter_tiles(self, map_name: str, zoom: str):
        """yield (x, y, path)，按 (y, x) 排序便于观看拼接过程。

        合并**所有**候选目录：抓取中的会话 → latest → 各历史会话 → 旧扁平目录，
        同一坐标以更靠前（更新）的为准。

        不能只读 ``resolve_dir()`` 返回的那一个目录：抓取会话里只存本次
        **新增/变化**的瓦片（没变化的由本地缓存直接应答、不写盘），拿会话目录
        当全集会把以前抓的瓦片全丢掉——"合成总图只有一小块"就是这么来的。
        """
        merged: dict[tuple[int, int], Path] = {}
        for d in self._compose_candidates(map_name, zoom):
            if not d.is_dir():
                continue
            try:
                entries = list(d.iterdir())
            except OSError as e:
                _log(f"读取瓦片目录失败 {d}: {e}")
                continue
            for f in entries:
                m = TILE_FILE_PATTERN.match(f.name)
                if not m or not f.is_file():
                    continue
                # setdefault：先到的优先级高（会话 > latest > 更老的会话）
                merged.setdefault((int(m.group(1)), int(m.group(2))), f)
        for (x, y), f in sorted(merged.items(), key=lambda kv: (kv[0][1], kv[0][0])):
            yield x, y, f


# =========================================================
# 会话合并（tools/merge_tiles.py 的移植）
# =========================================================

def merge_tiles(tiles_root: Path, run_prefix: str = RUN_PREFIX) -> dict:
    """把旧目录 + 所有会话的瓦片按坐标合并到 tiles/latest，返回统计"""
    stats = {"new": 0, "updated": 0, "unchanged": 0, "copied": 0}
    latest = Path(tiles_root) / "latest"
    sources: list[Path] = []

    def add_sources(root: Path):
        if not root.is_dir():
            return
        for map_dir in sorted(root.iterdir()):
            if not map_dir.is_dir():
                continue
            for zoom_dir in sorted(map_dir.iterdir()):
                if zoom_dir.is_dir():
                    sources.append(zoom_dir)

    # 旧版扁平目录
    for d in sorted(Path(tiles_root).iterdir()):
        if not d.is_dir() or d.name in ("latest", "maps", "marks", "icons"):
            continue
        if d.name.startswith(f"{run_prefix}_"):
            continue
        add_sources(d)
    # 会话目录（时间从旧到新，新的覆盖旧的）
    sessions = sorted(
        p for p in Path(tiles_root).iterdir()
        if p.is_dir() and p.name.startswith(f"{run_prefix}_")
    )
    for s in sessions:
        add_sources(s)

    for zoom_dir in sources:
        rel = Path(zoom_dir.parent.name) / zoom_dir.name
        for f in sorted(zoom_dir.iterdir()):
            if not f.is_file() or not TILE_FILE_PATTERN.match(f.name):
                continue
            dest = latest / rel / f.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                # 先比大小（不等必然不同，省掉一次全量读取），再比字节
                if (dest.stat().st_size == f.stat().st_size
                        and dest.read_bytes() == f.read_bytes()):
                    stats["unchanged"] += 1
                    continue
                stats["updated"] += 1
            else:
                stats["new"] += 1
            shutil.copy2(f, dest)
            stats["copied"] += 1
    return stats


# =========================================================
# 抓取会话状态
# =========================================================

class FetchState:
    def __init__(self, session_dir: Path, previous: Path | None):
        self.session_dir = Path(session_dir)
        self.previous = previous
        self.downloaded: set[str] = set()
        self.known: set[str] = set()  # 已下载过的瓦片 key（map/zoom/x_y），跨会话去重
        self.counts = {"new": 0, "updated": 0, "unchanged": 0, "error": 0, "skipped": 0}
        self.manifest = {"new": [], "updated": [], "errors": []}
        self.started = datetime.now()

    def save_manifest(self):
        if not (self.counts["new"] or self.counts["updated"]):
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
            "errors": self.manifest["errors"],
        }
        self.session_dir.mkdir(parents=True, exist_ok=True)
        (self.session_dir / "manifest.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )


# =========================================================
# 地图服务（供 HTTP Handler 调用）
# =========================================================

class MapService:
    def __init__(self, tiles_root: Path | None = None,
                 profile_dir: Path | None = None,
                 grid2d_dir: Path | None = None,
                 data_root: Path | None = None):
        #: 数据根目录：tiles/ grids2d/ browser_profile/ 的父目录
        self.data_root = Path(data_root) if data_root else default_data_root()
        self.bus = EventBus()
        self.store = TileStore(tiles_root or default_tiles_root(self.data_root))
        self.profile_dir = profile_dir or default_profile_dir(self.data_root)
        # 2D 网格（无高度）输出目录：可自定义（CLI --grid2d-dir / 环境变量 NAV_GRID2D_DIR）
        env_dir = os.environ.get("NAV_GRID2D_DIR", "")
        self.grid2d_dir = (grid2d_dir
                           or (Path(env_dir) if env_dir else None)
                           or self.data_root / "grids2d")
        self._lock = threading.Lock()
        self._fetch: FetchState | None = None
        self._fetch_thread: threading.Thread | None = None
        self._fetch_stop = threading.Event()
        self._fetch_headless = False
        self._compose: dict | None = None  # {"map","zoom","total","done"}
        self._simulate: dict | None = None
        self._simulation_cancel = threading.Event()
        self._sim_tiles: dict[tuple[int, int], bytes] = {}  # 模拟瓦片（内存）

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
        }

    # ---------- 事件 ----------
    def emit_sse(self, handler) -> None:
        """SSE 长连接：持续把事件推给浏览器，直到断开"""
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Connection", "keep-alive")
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
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", map_name):
            return None
        if not zoom.isdigit() or not (-1_000_000 <= x <= 1_000_000) \
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
                        state.manifest["errors"].append(str(e))
                        return
                    self._save_tile(state, url, content_type, data)
                except Exception as e:
                    state.counts["error"] += 1
                    state.manifest["errors"].append(str(e))
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
            state.manifest["errors"].append(str(e))
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
        x, y = int(xs), int(ys)
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
                state.counts["error"] += 1
                state.manifest["errors"].append(f"缺少 Pillow，无法转换 WebP: {rel}")
                return
            try:
                img = Image.open(BytesIO(data))
                img = img.convert("RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB")
                buf = BytesIO()
                img.save(buf, "PNG")
                data = buf.getvalue()
            except Exception as e:
                state.counts["error"] += 1
                state.manifest["errors"].append(f"WebP 转换失败 {rel}: {e}")
                return

        # 与上一次会话对比分类
        if state.previous is not None:
            prev_file = state.previous / rel
            if prev_file.exists():
                if prev_file.read_bytes() == data:
                    kind = "unchanged"
                else:
                    kind = "updated"
                    state.manifest["updated"].append(rel)
            else:
                kind = "new"
                state.manifest["new"].append(rel)
        else:
            kind = "new"
            state.manifest["new"].append(rel)

        dest = state.session_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        state.counts[kind] += 1
        state.known.add(key)  # 本次已抓到，后续重复请求不再处理

        self.bus.emit(type="tile", map=map_name, zoom=zoom, x=int(x), y=int(y),
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
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", map_name) or not zoom.isdigit():
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
            canvas = Image.new("RGB", (1, 1), (0, 0, 0)) if (save and Image) else None
            xs = [t[0] for t in tiles]
            ys = [t[1] for t in tiles]
            min_x, max_x, min_y, max_y = min(xs), max(xs), min(ys), max(ys)
            self.bus.emit(type="compose_bounds", map=map_name, zoom=zoom,
                          minX=min_x, maxX=max_x, minY=min_y, maxY=max_y)

            for i, (x, y, path) in enumerate(tiles, 1):
                try:
                    data = path.read_bytes()
                except OSError as e:
                    self.bus.emit(type="log", text=f"瓦片读取失败 {path.name}: {e}")
                    continue
                if canvas is not None:
                    try:
                        tile_img = Image.open(BytesIO(data)).convert("RGB")
                        if i == 1:  # 按边界建画布（注意 y 轴与瓦片一致，不翻转）
                            w = (max_x - min_x + 1) * TILE_SIZE
                            h = (max_y - min_y + 1) * TILE_SIZE
                            canvas = Image.new("RGB", (w, h), (0, 0, 0))
                        canvas.paste(tile_img, ((x - min_x) * TILE_SIZE,
                                                (y - min_y) * TILE_SIZE))
                    except Exception as e:
                        self.bus.emit(type="log", text=f"贴图失败 {path.name}: {e}")
                self.bus.emit(type="tile", map=map_name, zoom=zoom, x=x, y=y,
                              kind="existing", seq=i)
                if comp:
                    comp["done"] = i
                if i % 25 == 0:
                    self.bus.emit(type="compose_progress", map=map_name, zoom=zoom,
                                  done=i, total=len(tiles))

            if canvas is not None:
                # 按地图/zoom 分类存放，同名覆盖，只保留一份
                out = (self.store.tiles_root / "maps" / map_name / zoom
                       / f"{map_name}_{zoom}.png")
                out.parent.mkdir(parents=True, exist_ok=True)
                canvas.save(out)
                file_rel = f"{map_name}/{zoom}/{out.name}"
                self.bus.emit(type="log",
                              text=f"已保存拼接总图: tiles/maps/{file_rel}（覆盖旧版，只保留一份）")
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
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", map_name) or not zoom.isdigit():
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
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", map_name) or not zoom.isdigit():
            return None
        src = self.store.tiles_root / "maps" / map_name / zoom / f"{map_name}_{zoom}.png"
        if not src.is_file():
            return None
        return src.with_name(f"{map_name}_{zoom}.overview.png")

    def ensure_overview(self, map_name: str, zoom: str, max_px: int = 2048) -> Path | None:
        """生成/复用缩略总图（长边 <= max_px），返回缓存文件路径。

        前端以前是直接把总图整张下下来再画到同尺寸画布上：map02@4 的文件
        78.9 MB，解码后是 10752x15872 的位图，约 685 MB。这是"打开地图就卡"
        的主因。缩略图按 2048 长边生成后只有几百 KB，解码后几 MB，缩着看
        完全够用；放大到瓦片级时前端另外按需取瓦片。

        生成一次后按 mtime 复用；总图重新合成后才需要再生成。
        """
        out = self.overview_path(map_name, zoom)
        if out is None:
            return None
        if Image is None:
            return None
        src = self.store.tiles_root / "maps" / map_name / zoom / f"{map_name}_{zoom}.png"
        try:
            if out.is_file() and out.stat().st_mtime >= src.stat().st_mtime:
                return out
            img = Image.open(src)
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.thumbnail((max_px, max_px), Image.Resampling.BILINEAR)
            tmp = out.with_name(out.name + ".tmp")
            img.save(tmp, "PNG", optimize=True)
            os.replace(tmp, out)
            _log(f"缩略总图已生成 {out.name} ({img.width}x{img.height})")
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
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", map_name) or not zoom.isdigit():
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
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", map_name) or not zoom.isdigit():
            return {"ok": False, "error": "非法地图名/zoom"}
        if not points or len(points) < 3:
            return {"ok": False, "error": "至少需要 3 个启用中的控制点"}
        pixel, world, metas = [], [], []
        for p in points:
            px, py = p.get("pixel", [None, None])
            wx, wz = p.get("world", [None, None])
            if None in (px, py, wx, wz):
                continue
            pixel.append([float(px), float(py)])
            world.append([float(wx), float(wz)])
            metas.append(bool(p.get("enabled", True)))
        if sum(metas) < 3:
            return {"ok": False, "error": "至少需要 3 个启用中的控制点"}

        # 只用启用点参与拟合；RANSAC 剔除离群点，再求最小二乘
        use_pixel = [pixel[i] for i in range(len(pixel)) if metas[i]]
        use_world = [world[i] for i in range(len(world)) if metas[i]]
        try:
            matrix, inlier_flags = _fit_affine_ransac(use_pixel, use_world, threshold)
        except ValueError as e:
            return {"ok": False, "error": f"拟合失败: {e}"}
        inverse_matrix = _invert_affine(matrix)

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
            enabled = False
            if metas[i]:
                enabled = bool(inlier_flags[j])
                j += 1
            control_points.append({
                "pixel": [pixel[i][0], pixel[i][1]],
                "world": [world[i][0], world[i][1]],
                "enabled": enabled,
                "error": round(errs[i], 4),
            })

        mapping = {
            "map_name": f"{map_name}_{zoom}.png",
            "image_size": {
                "width": int(image_size.get("width", 0)),
                "height": int(image_size.get("height", 0)),
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
        out.write_text(json.dumps(mapping, ensure_ascii=False, indent=2),
                       encoding="utf-8")
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
        # map_name 里带 / 或 .. 就能读到 grids2d/ 之外的同格式文件。
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", map_name) or not zoom.isdigit():
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
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", map_name) or not zoom.isdigit():
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
            if not (cell_size > 0):
                raise ValueError("cell_size 必须大于 0")
        except (KeyError, TypeError, ValueError, IndexError):
            return {"ok": False, "error": "origin/cell_size 缺失或非法"}

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
                             "（要清空请直接删掉 grids2d/ 下的文件）"}
        width = max_ix - min_ix + 1   # 列数 = x 方向
        height = max_iz - min_iz + 1  # 行数 = z 方向
        if width * height > DENSE_CELL_LIMIT:
            return {"ok": False,
                    "error": f"格子跨度 {width}×{height} 超过稠密化上限（{DENSE_CELL_LIMIT} 格），"
                             "请把格子放在更集中的区域"}

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
        # 先写临时文件再原子替换：服务是多线程的，两次保存撞上会写出半个 npz
        tmp = out.with_name(out.name + ".tmp")
        with open(tmp, "wb") as fh:
            # meta 用 numpy 字符串数组存（0 维），读方才能不开 allow_pickle
            np.savez_compressed(fh, cells=grid,
                                meta=np.array(json.dumps(meta, ensure_ascii=False)))
        # 导出自检：用与 ok-end-field 读方同级的检查读回临时文件，通过才替换。
        # 宁可保存失败也不能覆盖成读不回来的文件。
        try:
            _load_grid_npz(tmp)
        except Exception as e:
            tmp.unlink(missing_ok=True)
            _log(f"2D 网格导出自检失败 {out}: {e}")
            return {"ok": False, "error": f"导出自检失败（未覆盖原文件）: {e}"}
        os.replace(tmp, out)
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

def _load_grid_npz(path: Path) -> tuple[np.ndarray, dict, list[str]]:
    """严格读取 ``*.grid.npz``，返回 ``(cells, meta, warnings)``。

    检查项与 ok-end-field 的 ``src/nav/grid_io.py`` +
    ``scripts/nav/verify_grid.py`` 对齐：读方会拒的文件，编辑器也必须拒。
    否则会把"规划器读不动的文件"画成一张看起来正常的图，用户直到跑规划
    才发现数据不对。warnings 是不影响可读性的可疑点（轴约定文字、
    map_name/zoom 缺失等），由前端提示。
    """
    with np.load(path, allow_pickle=False) as z:
        members = sorted(z.files)
        if members != sorted(GRID_MEMBERS):
            raise ValueError(f"npz 成员应为 {sorted(GRID_MEMBERS)}，实际 {members}")
        arr = np.asarray(z["cells"])
        meta_raw = np.asarray(z["meta"])

    if arr.dtype != np.uint8:
        raise ValueError(f"cells dtype 应为 uint8，实际 {arr.dtype}")
    if arr.ndim != 2 or arr.size == 0:
        raise ValueError(f"cells 形状非法: {arr.shape}")
    bad = sorted(set(np.unique(arr).tolist()) - set(GRID_STATES))
    if bad:
        raise ValueError(f"cells 出现非法取值 {bad}（只允许 0=未知 / 1=可走 / 2=阻挡）")

    if meta_raw.dtype.kind not in ("U", "S") or meta_raw.size != 1:
        raise ValueError("meta 必须是单个 numpy 字符串数组"
                         f"（np.array(json.dumps(...))），"
                         f"实际 dtype={meta_raw.dtype} size={meta_raw.size}")
    text = meta_raw.item()
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    meta = json.loads(text)
    if not isinstance(meta, dict):
        raise ValueError("meta 顶层必须是 JSON 对象")
    if meta.get("magic") != GRID_MAGIC:
        raise ValueError(f"不是本项目的网格文件（magic={meta.get('magic')!r}）")
    if int(meta.get("schema_version") or 0) != GRID_SCHEMA_VERSION:
        raise ValueError(f"网格版本不匹配（文件 {meta.get('schema_version')}，"
                         f"当前 {GRID_SCHEMA_VERSION}）")
    try:
        cell_size = float(meta.get("cell_size"))
    except (TypeError, ValueError):
        raise ValueError(f"cell_size 必须是数字，实际 {meta.get('cell_size')!r}") from None
    if not cell_size > 0:
        raise ValueError(f"cell_size 必须大于 0，实际 {cell_size}")

    origin = meta.get("origin")
    if not (isinstance(origin, (list, tuple)) and len(origin) == 3
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in origin)):
        raise ValueError(f"origin 应是 3 个数字，实际 {origin!r}")

    warnings: list[str] = []
    if str(meta.get("axis_convention") or "") != GRID_AXIS_CONVENTION:
        warnings.append("axis_convention 与规范文字不一致（几何约定可能不同）")
    for key in ("map_name", "zoom"):
        if not str(meta.get(key) or "").strip():
            warnings.append(f"meta 缺少 {key}")
    return arr, meta, warnings

# =========================================================
# 纯 Python 仿射拟合（不借助 numpy）——供地图标定使用
# =========================================================

def _mat_vec(m2x3, x, y):
    """2x3 仿射矩阵 [x, y, 1] 相乘"""
    return (m2x3[0][0] * x + m2x3[0][1] * y + m2x3[0][2],
            m2x3[1][0] * x + m2x3[1][1] * y + m2x3[1][2])


def _gauss_solve(a, b):
    """高斯消元（部分主元）解线性方程组 a x = b"""
    n = len(b)
    m = [a[i][:] + [b[i]] for i in range(n)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            raise ValueError("奇异矩阵：控制点可能共线/重合")
        m[col], m[piv] = m[piv], m[col]
        pv = m[col][col]
        for j in range(col, n + 1):
            m[col][j] /= pv
        for r in range(n):
            if r == col:
                continue
            f = m[r][col]
            if abs(f) < 1e-15:
                continue
            for j in range(col, n + 1):
                m[r][j] -= f * m[col][j]
    return [m[i][n] for i in range(n)]


def _solve_affine(pixel, world):
    """最小二乘解 像素→世界 的 2x3 仿射矩阵（正规方程 + 高斯消元）"""
    n = len(pixel)
    if n < 3:
        raise ValueError("至少需要 3 个控制点")
    ata = [[0.0] * 6 for _ in range(6)]
    atb = [0.0] * 6
    for (px, py), (wx, wy) in zip(pixel, world):
        r1 = [px, py, 1.0, 0.0, 0.0, 0.0]
        for j in range(6):
            atb[j] += r1[j] * wx
            for k in range(6):
                ata[j][k] += r1[j] * r1[k]
        r2 = [0.0, 0.0, 0.0, px, py, 1.0]
        for j in range(6):
            atb[j] += r2[j] * wy
            for k in range(6):
                ata[j][k] += r2[j] * r2[k]
    s = _gauss_solve(ata, atb)
    return [[s[0], s[1], s[2]], [s[3], s[4], s[5]]]


def _invert_affine(m):
    """2x3 仿射矩阵解析求逆（世界→像素）"""
    a, b, c = m[0]
    d, e, f = m[1]
    det = a * e - b * d
    if abs(det) < 1e-12:
        raise ValueError("矩阵不可逆（退化仿射）")
    return [[e / det, -b / det, (b * f - e * c) / det],
            [-d / det, a / det, (d * c - a * f) / det]]


def _fit_affine_ransac(pixel, world, threshold=5.0, iterations=120, seed=7):
    """RANSAC 拟合：随机 3 点抽样，取内点最多的模型，内点最小二乘重估。

    返回 (matrix, inlier_flags)，inlier_flags 与 pixel 等长。
    """
    import random
    rng = random.Random(seed)
    n = len(pixel)
    best_inliers = []
    for _ in range(iterations):
        idx = rng.sample(range(n), 3)
        try:
            m = _solve_affine([pixel[i] for i in idx], [world[i] for i in idx])
        except ValueError:
            continue
        inl = []
        for i in range(n):
            wx, wz = _mat_vec(m, pixel[i][0], pixel[i][1])
            d = ((wx - world[i][0]) ** 2 + (wz - world[i][1]) ** 2) ** 0.5
            if d <= threshold:
                inl.append(i)
        if len(inl) > len(best_inliers):
            best_inliers = inl
    if not best_inliers:
        best_inliers = list(range(n))  # 全军覆没时退化为全点最小二乘
    matrix = _solve_affine([pixel[i] for i in best_inliers],
                           [world[i] for i in best_inliers])
    flags = [i in best_inliers for i in range(n)]
    return matrix, flags
