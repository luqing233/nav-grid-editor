# -*- coding: utf-8 -*-
"""Official layered-map metadata for the editor.

The official map exposes each floor as a full-size transparent PNG.  The
coordinates below are the official ``bounds`` in world X/Z space.  Only the
curated catalog is accepted; callers cannot turn arbitrary paths into cache
locations.

``assets/tiles/layers/`` is a local cache and never enters the repository, so
``catalog.json`` is usually absent on a fresh checkout.  The built-in seed
below mirrors exactly what ``fetch-layers`` generates for Map02Lv002 --
including ``id == logic_id`` and the ``map02/<level>/`` cache layout -- so the
two sources stay interchangeable.  ``tests/test_layers.py`` pins those
invariants; the seed is a fallback, not a second format.
"""

from __future__ import annotations

import json
from pathlib import Path

from .settings import valid_map_zoom


CATALOG_FILENAME = "catalog.json"

#: 分层 PNG 的下载上限。服务端按需缓存（``MapService.get_layer_image``）与
#: ``fetch-layers`` 预抓取必须用同一个值，否则中间尺寸的图会「CLI 抓得到、
#: 服务端当失败」，缓存目录里留下半个状态。
MAX_LAYER_IMAGE_BYTES = 12 * 1024 * 1024

_ASSET_BASE = "https://assets.skland.com/game-map/endfield/"

_MAP02_LV002_BOUNDS = {
    "west": -1664.0,
    "east": -512.0,
    "south": -1024.0,
    "north": 384.0,
}

# 官方分层入口标记（templateId=feec2fbd…）里 targetRegionId 指向本组的那几条。
_MAP02_LV002_ENTRANCES = {
    "储备站": (
        {"x": -837.0, "z": -844.0, "radius": 28.0},
        {"x": -830.0, "z": -796.0, "radius": 28.0},
        {"x": -756.0, "z": -794.0, "radius": 28.0},
    ),
    "天井院": (
        {"x": -800.0, "z": -536.0, "radius": 28.0},
        {"x": -752.0, "z": -534.0, "radius": 28.0},
    ),
}

# regionSet id：group_key 用得到，也是官方入口标记的 targetRegionId。
_MAP02_LV002_SETS = {"储备站": "18", "天井院": "19"}

_MAP02_LV002_LEVEL_ID = "map02_lv002"
_MAP02_LV002_LEVEL_NAME = "武陵城"
_MAP02_LV002_REGION_VERSION = "1_2"

# (logic_id, 名称, 组, tier_index, 官方图标名, region_id, sort_order)
# 图标后缀跟着官方 logicId 走，**不能**由 tier_index 推出来。
_MAP02_LV002_ROWS = (
    ("scene_map02_lv002_layer_tips_3", "储备站二层", "储备站", 1,
     "Map02Lv002/tier_02_002_2", 254, 0),
    ("scene_map02_lv002_layer_tips_2", "储备站一层", "储备站", -1,
     "Map02Lv002/tier_02_002_3", 255, 1),
    ("scene_map02_lv002_layer_tips_1", "储备站地下室", "储备站", -2,
     "Map02Lv002/tier_02_002_1", 275, 2),
    ("scene_map02_lv002_layer_tips_4", "天井院地下一层", "天井院", -1,
     "Map02Lv002/tier_02_002_4", 259, 100),
    ("scene_map02_lv002_layer_tips_5", "天井院地下二层", "天井院", -2,
     "Map02Lv002/tier_02_002_5", 258, 101),
    ("scene_map02_lv002_layer_tips_6", "天井院地下三层", "天井院", -3,
     "Map02Lv002/tier_02_002_6", 257, 102),
    ("scene_map02_lv002_layer_tips_7", "天井院底部", "天井院", -4,
     "Map02Lv002/tier_02_002_7", 276, 103),
)


def _map02_lv002_layer(logic_id: str, name: str, group: str, tier_index: int,
                       icon: str, region_id: int, sort_order: int) -> dict:
    entrances = [dict(item) for item in _MAP02_LV002_ENTRANCES[group]]
    set_id = _MAP02_LV002_SETS[group]
    filename = f"{logic_id}.png"
    return {
        "id": logic_id,
        "map_name": "map02",
        "map_display_name": "武陵",
        "map_category": "layered",
        "level_id": _MAP02_LV002_LEVEL_ID,
        "level_name": _MAP02_LV002_LEVEL_NAME,
        "region_set_id": set_id,
        "region_id": region_id,
        "group": group,
        "group_key": f"{_MAP02_LV002_LEVEL_ID}:{set_id}",
        "name": name,
        "tier_index": tier_index,
        "logic_id": logic_id,
        "icon": icon,
        "filename": filename,
        "path": f"map02/{_MAP02_LV002_LEVEL_ID}/{filename}",
        "source_url": f"{_ASSET_BASE}region_{_MAP02_LV002_REGION_VERSION}/{icon}.png",
        "region_version": _MAP02_LV002_REGION_VERSION,
        "bounds": dict(_MAP02_LV002_BOUNDS),
        "entrance": dict(entrances[0]),
        "entrances": entrances,
        "sort_order": sort_order,
        "width": 5400,
        "height": 6600,
    }


LAYER_CATALOG: dict[str, tuple[dict, ...]] = {
    "map02": tuple(_map02_lv002_layer(*row) for row in _MAP02_LV002_ROWS),
}


def builtin_layer_entries() -> list[dict]:
    return [
        {**item, "bounds": dict(item["bounds"]),
         "entrance": dict(item.get("entrance") or {})}
        for items in LAYER_CATALOG.values()
        for item in items
    ]


def load_layer_catalog(path: Path) -> list[dict]:
    """Load a generated layer catalog and validate its top-level shape."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    layers = data.get("layers") if isinstance(data, dict) else data
    if not isinstance(layers, list):
        raise ValueError("catalog layers must be an array")
    out = []
    for item in layers:
        if not isinstance(item, dict) or not item.get("id"):
            raise ValueError("catalog contains an invalid layer entry")
        out.append(item)
    return out


def list_layers(map_name: str, zoom: str,
                entries: list[dict] | None = None) -> list[dict]:
    """Return layer metadata for a map/zoom, or an empty list."""
    if not valid_map_zoom(map_name, zoom):
        return []
    source = builtin_layer_entries() if entries is None else entries
    return [
        {**item, "bounds": dict(item.get("bounds") or {}),
         "entrance": dict(item.get("entrance") or {})}
        for item in source
        if item.get("map_name") == map_name
    ]


def find_layer(map_name: str, zoom: str, layer_id: str,
               entries: list[dict] | None = None) -> dict | None:
    """Return one catalog entry, or None for unknown input."""
    if not valid_map_zoom(map_name, zoom):
        return None
    source = builtin_layer_entries() if entries is None else entries
    for item in source:
        if item.get("map_name") != map_name:
            continue
        if item["id"] == layer_id:
            return item
    return None


def layer_source_url(entry: dict) -> str:
    """Official CDN URL for a layer entry.

    生成目录总是带 ``source_url``；这里的回退按官方同一套命名拼
    （``region_<版本>/<图标名>.png``），好让手写的 catalog 与抓取结果一致。
    """
    if entry.get("source_url"):
        return str(entry["source_url"])
    icon = str(entry.get("icon") or "").strip()
    if not icon:
        raise ValueError("layer entry has neither source_url nor icon")
    segment = f"region_{entry['region_version']}" if entry.get("region_version") else "region"
    return f"{_ASSET_BASE}{segment}/{icon}.png"


def layer_cache_relpath(entry: dict) -> Path:
    """Path under ``assets/tiles`` used for the local layer cache."""
    raw = entry.get("path")
    if not raw:
        level_id = str(entry.get("level_id") or "").strip()
        filename = str(entry.get("filename") or f"{entry['id']}.png")
        parts = [str(entry.get("map_name") or ""), level_id, filename]
        raw = "/".join(part for part in parts if part)
    path = Path(str(raw))
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("layer cache path escapes tiles root")
    return Path("layers") / path
