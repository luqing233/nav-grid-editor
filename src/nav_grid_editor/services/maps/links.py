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
from .settings import valid_grid_tier, valid_map_name


LINK_SCHEMA_VERSION = 1
LINK_KINDS = frozenset(("stairs", "elevator", "portal", "door", "rope", "other"))
_links_guard = threading.RLock()


def links_path(grid2d_dir: Path) -> Path:
    return Path(grid2d_dir) / "links.json"


def empty_links() -> dict:
    return {"schema_version": LINK_SCHEMA_VERSION, "links": []}


def load_links(path: Path) -> dict:
    path = Path(path)
    if not path.is_file():
        return empty_links()
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("links"), list):
        raise ValueError("links.json 顶层必须是 {schema_version, links}")
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


def _endpoint(raw) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("连接端点必须是对象")
    map_name = str(raw.get("map") or "")
    tier = str(raw.get("tier") or "")
    if not valid_map_name(map_name) or not valid_grid_tier(tier):
        raise ValueError("连接端点 map/tier 非法")
    return {"map": map_name, "tier": tier, "rect": _finite_rect(raw.get("rect"))}


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
