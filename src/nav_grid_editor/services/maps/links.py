# -*- coding: utf-8 -*-
"""Persistent cross-layer grid links (the navigation "vias")."""

from __future__ import annotations

import json
import math
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .io import _path_lock, _replace_with_retry, _unique_tmp
from .settings import valid_grid_tier, valid_map_name, valid_map_zoom


# v2 起端点带 zoom：网格文件是按 <地图>__<tier>_<zoom>.grid.npz 分档的，缺了
# zoom 消费方只能硬编码。v3 起端点可带高度范围（可选），见 _optional_y_range。
LINK_SCHEMA_VERSION = 3
LINK_KINDS = frozenset(("stairs", "elevator", "portal", "door", "rope", "other"))
_links_guard = threading.RLock()


def links_path(grid2d_dir: Path) -> Path:
    return Path(grid2d_dir) / "links.json"


def empty_links() -> dict:
    return {"schema_version": LINK_SCHEMA_VERSION, "links": []}


def load_links(path: Path) -> dict:
    """读连接，并把内存里的形状统一到当前 schema。

    v1 的端点没有 zoom，补齐成 ""（含义：按查询的 zoom 解析，见
    ``MapService._link_endpoint_grid``）；v1/v2 都没有可选的端点高度，缺了就保持
    缺失（"不知道"是个合法状态，不能编一个）。schema_version 一律报当前值 ——
    调用方拿到的永远是同一套形状，不用分版本分支。磁盘上的版本等下一次写入才升上去。
    """
    path = Path(path)
    if not path.is_file():
        return empty_links()
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("links"), list):
        raise ValueError("links.json 顶层必须是 {schema_version, links}")
    for link in data["links"]:
        if not isinstance(link, dict):
            continue
        for side in ("from", "to"):
            endpoint = link.get(side)
            if isinstance(endpoint, dict) and "zoom" not in endpoint:
                link[side] = {**endpoint, "zoom": ""}
    data["schema_version"] = LINK_SCHEMA_VERSION
    return data


def _finite_rect(raw) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("连接区域必须是对象")
    try:
        x0, z0 = float(raw["x0"]), float(raw["z0"])
        x1, z1 = float(raw["x1"]), float(raw["z1"])
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ValueError("连接区域 x0/z0/x1/z1 必须是数字") from None
    if not all(math.isfinite(v) for v in (x0, z0, x1, z1)):
        raise ValueError("连接区域必须是有限数")
    if x1 <= x0 or z1 <= z0:
        raise ValueError("连接区域必须满足 x1>x0 且 z1>z0")
    return {"x0": x0, "z0": z0, "x1": x1, "z1": z1}


def _optional_y_range(raw: dict) -> dict:
    """端点的可选高度范围，返回 ``{"y_min":…, "y_max":…}`` 或空字典。

    为什么需要它：端点过去只有 X/Z，于是「这两点是不是通向同一处空间」在格式层面
    **无法校验**，两层的落差也只能从各自的 `floor_regions` 区间去推 —— 那是区间不是
    点，"按落差定价"更无从下手。加上这个范围后，落差可以是量出来的。

    可选是因为老数据没有；两个键要么都给、要么都不给（只给一个基本是标错了）。
    """
    has_min = "y_min" in raw
    has_max = "y_max" in raw
    if not has_min and not has_max:
        return {}
    if not (has_min and has_max):
        raise ValueError("端点高度要么 y_min/y_max 都给，要么都不给")
    try:
        y_min, y_max = float(raw["y_min"]), float(raw["y_max"])
    except (TypeError, ValueError, OverflowError):
        raise ValueError("端点高度必须是数字") from None
    if not (math.isfinite(y_min) and math.isfinite(y_max)):
        raise ValueError("端点高度必须是有限数")
    if y_min > y_max:
        raise ValueError("端点 y_min 不能大于 y_max")
    return {"y_min": y_min, "y_max": y_max}


def _endpoint(raw) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("连接端点必须是对象")
    map_name = str(raw.get("map") or "")
    tier = str(raw.get("tier") or "")
    if not valid_map_name(map_name) or not valid_grid_tier(tier):
        raise ValueError("连接端点 map/tier 非法")
    # zoom 允许为空，含义是「该层只有一个网格档次时按它解析」。为空时由
    # MapService 决定怎么落文件；有多档就报错要求重存，不猜。
    zoom = str(raw.get("zoom") or "")
    if zoom and not valid_map_zoom(map_name, zoom):
        raise ValueError("连接端点 zoom 非法")
    endpoint = {
        "map": map_name, "tier": tier, "zoom": zoom,
        "rect": _finite_rect(raw.get("rect")),
    }
    endpoint.update(_optional_y_range(raw))
    return endpoint


def normalize_link(raw: dict, link_id: str = "") -> dict:
    if not isinstance(raw, dict):
        raise ValueError("连接必须是对象")
    source = _endpoint(raw.get("from"))
    target = _endpoint(raw.get("to"))
    if source == target:
        raise ValueError("起点和终点不能完全相同")
    kind = str(raw.get("kind") or "stairs")
    if kind not in LINK_KINDS:
        raise ValueError(f"连接类型必须是 {sorted(LINK_KINDS)}")
    try:
        cost = float(raw.get("cost", 1.0))
    except (TypeError, ValueError, OverflowError):
        raise ValueError("连接代价必须是正数") from None
    if not math.isfinite(cost) or cost <= 0:
        raise ValueError("连接代价必须是正数")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "id": link_id or str(raw.get("id") or uuid4().hex),
        "from": source,
        "to": target,
        "bidirectional": bool(raw.get("bidirectional", True)),
        "kind": kind,
        "cost": cost,
        "label": str(raw.get("label") or "")[:120],
        "updated_at": now,
    }


def write_links(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _unique_tmp(path)
    with _path_lock(path):
        try:
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            _replace_with_retry(tmp, path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise


def upsert_link(path: Path, raw: dict) -> dict:
    path = Path(path)
    with _links_guard:
        data = load_links(path)
        link_id = str(raw.get("id") or "")
        normalized = normalize_link(raw, link_id)
        normalized.setdefault("created_at", normalized["updated_at"])
        replaced = False
        for i, item in enumerate(data["links"]):
            if str(item.get("id") or "") == normalized["id"]:
                normalized["created_at"] = (
                    item.get("created_at") or normalized["updated_at"]
                )
                data["links"][i] = normalized
                replaced = True
                break
        if not replaced:
            data["links"].append(normalized)
        data["schema_version"] = LINK_SCHEMA_VERSION
        write_links(path, data)
        return normalized


def delete_link(path: Path, link_id: str) -> bool:
    path = Path(path)
    with _links_guard:
        data = load_links(path)
        links = [
            item for item in data["links"]
            if str(item.get("id") or "") != link_id
        ]
        changed = len(links) != len(data["links"])
        if changed:
            data["links"] = links
            write_links(path, data)
        return changed
