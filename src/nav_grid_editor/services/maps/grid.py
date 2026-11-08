# -*- coding: utf-8 -*-
"""2D 导航网格 npz 的严格读写。"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

import numpy as np

from .settings import (
    GRID_AXIS_CONVENTION,
    GRID_MAGIC,
    GRID_MEMBERS,
    GRID_SCHEMA_VERSION,
    GRID_STATES,
)


_MAX_FLOOR_REGIONS = 256
_MAX_FLOOR_REGION_CELLS = 50_000


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


def _load_grid_npz(path: Path) -> tuple[np.ndarray, dict, list[str]]:
    """严格读取 ``*.grid.npz``，返回 ``(cells, meta, warnings)``。

    并发的保存正在 os.replace 这个文件时，Windows 会短暂拒绝打开（替换要求
    目标独占）。这不是数据损坏——替换是原子的，读到的要么是旧版本要么是新
    版本，不会是写了一半的——所以退避重试即可。
    """
    for attempt in range(6):
        try:
            return _load_grid_npz_once(path)
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.05 * (attempt + 1))


def _load_grid_meta_npz(path: Path) -> dict:
    """Read only the JSON metadata member of a grid file."""
    for attempt in range(6):
        try:
            with np.load(path, allow_pickle=False) as z:
                members = sorted(z.files)
                if members != sorted(GRID_MEMBERS):
                    raise ValueError(
                        f"npz 成员应为 {sorted(GRID_MEMBERS)}，实际 {members}")
                meta_raw = np.asarray(z["meta"])
            break
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.05 * (attempt + 1))

    if meta_raw.dtype.kind not in ("U", "S") or meta_raw.size != 1:
        raise ValueError("meta 必须是单个 numpy 字符串数组")
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
    return meta


def _load_grid_npz_once(path: Path) -> tuple[np.ndarray, dict, list[str]]:
    """真正做校验的读方（重试逻辑见 :func:`_load_grid_npz`）。

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
    if not math.isfinite(cell_size) or cell_size <= 0:
        raise ValueError(f"cell_size 必须是有限正数，实际 {cell_size}")

    origin = meta.get("origin")
    if not (isinstance(origin, (list, tuple)) and len(origin) == 3
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in origin)):
        raise ValueError(f"origin 应是 3 个数字，实际 {origin!r}")
    # 别的工具写出的 NaN/inf origin 也要拒：json.loads 接受非标准字面量，
    # 放行的话整张网格的世界坐标会全变成 nan
    if not all(math.isfinite(float(v)) for v in origin):
        raise ValueError(f"origin 必须都是有限数，实际 {origin!r}")

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
