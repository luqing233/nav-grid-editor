# -*- coding: utf-8 -*-
"""瓦片目录解析、读取与增量会话合并。"""

from __future__ import annotations

import shutil
from pathlib import Path

from .io import _log, _path_lock, _replace_with_retry, _unique_tmp
from .settings import RUN_PREFIX, TILE_FILE_PATTERN, valid_map_zoom


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
        # 兜底：这两个值会被拼进文件系统路径，非法值直接当作"没有瓦片"，
        # 免得任何调用方漏校验就变成路径穿越
        if not valid_map_zoom(map_name, zoom):
            return []
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
            # 先拷到临时文件再原子替换：合并跑在抓取线程上，而 HTTP 线程同时在
            # 读 tiles/latest（/tiles/ 路由、合成）——直接 copy2 会让读者读到
            # 写了一半的 PNG，解码失败后被当成"这张瓦片缺失"
            tmp = _unique_tmp(dest)
            with _path_lock(dest):
                try:
                    shutil.copy2(f, tmp)
                    _replace_with_retry(tmp, dest)
                except Exception:
                    tmp.unlink(missing_ok=True)
                    raise
            stats["copied"] += 1
    return stats


# =========================================================
# 抓取会话状态
# =========================================================
