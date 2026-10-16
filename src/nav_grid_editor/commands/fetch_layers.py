# -*- coding: utf-8 -*-
"""Discover and download every official layered-map image."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib import request

from ..paths import PROJECT_ROOT
from ..services.maps.layers import MAX_LAYER_IMAGE_BYTES

try:
    from PIL import Image
except Exception:  # pragma: no cover - runtime dependency is normally installed
    Image = None


PAGE_URL = "https://game.skland.com/map/endfield"
TREE_URL = "https://zonai.skland.com/web/v1/game/endfield/map/tree"
ASSET_BASE = "https://assets.skland.com/game-map/endfield/"
LAYER_ENTRANCE_TID = "feec2fbd748b3d10131a07971b25baf2"
MAP_DISPLAY_NAMES = {
    "base01": "帝江号",
    "map01": "四号谷地",
    "map02": "武陵",
}
INDEPENDENT_MAP_NAMES = {
    "indie_dg007": "首墩内部",
    "indie_dg016": "遂明",
}
MAX_RESPONSE_BYTES = 24 * 1024 * 1024  # 页面 / map tree / webpack chunk 的通用上限
HEADERS = {
    "User-Agent": "Mozilla/5.0 nav-grid-editor fetch-layers",
    "Referer": PAGE_URL,
}


def _fetch_bytes(url: str, timeout: int = 45,
                 max_bytes: int = MAX_RESPONSE_BYTES) -> bytes:
    req = request.Request(url, headers=HEADERS)
    with request.urlopen(req, timeout=timeout) as resp:
        if getattr(resp, "status", 200) != 200:
            raise OSError(f"HTTP {resp.status}: {url}")
        data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise OSError(f"response too large: {url}")
    return data


def _fetch_text(url: str) -> str:
    return _fetch_bytes(url).decode("utf-8", errors="replace")


def _extract_balanced_object(text: str, start: int) -> str:
    if start < 0 or start >= len(text) or text[start] != "{":
        raise ValueError("object start must point at '{'")
    depth = 0
    quote = None
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise ValueError("unclosed JavaScript object")


def _js_object_to_python(text: str):
    quoted = re.sub(
        r"([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*:)",
        r'\1"\2"\3',
        text,
    )
    return json.loads(quoted)


def _extract_mapping(text: str) -> dict:
    idx = text.find("scene_")
    if idx < 0:
        raise ValueError("layer mapping not found")
    start = text.rfind("{", 0, idx)
    return _js_object_to_python(_extract_balanced_object(text, start))


def _extract_named_object(text: str, name: str) -> dict:
    match = re.search(re.escape(name) + r"\s*:\s*\{", text)
    if not match:
        raise ValueError(f"{name} not found")
    start = text.find("{", match.start())
    return _js_object_to_python(_extract_balanced_object(text, start))


def _discover_bundle(cache_dir: Path, log=print) -> tuple[dict, dict, dict]:
    """Return ``(mapping, region_versions, source_info)`` from the live bundle."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    page = _fetch_text(PAGE_URL)
    script_urls = re.findall(r'<script[^>]+src="([^"]+\.js)"', page)
    main_url = next(
        (url for url in script_urls if "skland-game-map/main." in url),
        "",
    )
    if not main_url:
        raise RuntimeError("official main.js not found in page")
    main_text = _fetch_text(main_url)
    base_match = re.search(r'f\.p="([^"]+)"', main_text)
    if not base_match:
        raise RuntimeError("webpack static base not found")
    base_url = base_match.group(1)
    chunk_match = re.search(
        r"f\.u=function\(e\)\{return\(547===e\?\"bootstrap\":e\)\+"
        r"\"\.\"\+\{([^}]+)\}\[e\]\+\"\.js\"\}",
        main_text,
    )
    if not chunk_match:
        raise RuntimeError("webpack chunk map not found")
    chunk_hashes = dict(re.findall(
        r'(\d+):"([0-9a-f]+)"', chunk_match.group(1)
    ))
    chunks = {}
    if "547" in chunk_hashes:
        chunks["bootstrap"] = chunk_hashes.pop("547")
    chunks.update(chunk_hashes)

    def fetch_chunk(name: str, digest: str):
        filename = (
            f"{name}.{digest}.js"
            if name != "bootstrap"
            else f"bootstrap.{digest}.js"
        )
        cached = cache_dir / filename
        if cached.is_file():
            return filename, cached.read_text(encoding="utf-8"), None
        try:
            text = _fetch_text(base_url + filename)
        except Exception as e:  # noqa: BLE001
            return filename, "", str(e)
        cached.write_text(text, encoding="utf-8")
        return filename, text, None

    mapping = None
    region_versions: dict = {}
    mapping_source = ""
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(fetch_chunk, name, digest)
            for name, digest in chunks.items()
        ]
        for future in concurrent.futures.as_completed(futures):
            filename, text, error = future.result()
            if error:
                log(f"warning: bundle chunk skipped {filename}: {error}")
                continue
            if mapping is None and "scene_" in text:
                try:
                    mapping = _extract_mapping(text)
                    mapping_source = filename
                except Exception:
                    pass
            if not region_versions and "regionVersionMap" in text:
                try:
                    region_versions = _extract_named_object(
                        text, "regionVersionMap"
                    )
                except Exception:
                    pass
    if mapping is None:
        raise RuntimeError("official layer mapping not found in bundle chunks")
    if not region_versions:
        log("warning: regionVersionMap not found; using region/ paths")
    return mapping, region_versions, {
        "page_url": PAGE_URL,
        "main_js": main_url,
        "mapping_chunk": mapping_source,
    }


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return None


def _strip_floor_suffix(name: str) -> str:
    return re.sub(
        r"(地?下?[一二三四五六七八九十0-9]+层|地下室|底部|顶层|上层|下层|"
        r"二层|一层|三层|四层|内部|主区|副区)$",
        "",
        name.strip(),
    )


def _group_name(level_name: str, regions: list[dict], set_id: str) -> str:
    names = [str(r.get("name") or "").strip() for r in regions]
    names = [name for name in names if name]
    if len(names) == 1:
        group = _strip_floor_suffix(names[0])
        if group:
            return group
    if names:
        prefix = names[0]
        for name in names[1:]:
            n = 0
            while n < len(prefix) and n < len(name) and prefix[n] == name[n]:
                n += 1
            prefix = prefix[:n]
        prefix = re.sub(r"(地下|地上|地面|内部)$", "", prefix.strip())
        if len(prefix) >= 2:
            return prefix
    return f"{level_name} · {set_id}"


def _nearest_entrance(points: list[dict], x: float, z: float,
                      radius: float = 300.0) -> dict | None:
    best = None
    best_d = radius
    for point in points:
        if (
            point.get("t") != LAYER_ENTRANCE_TID
            and point.get("templateId") != LAYER_ENTRANCE_TID
        ):
            continue
        pos = point.get("pos") or point
        if "x" not in pos or "z" not in pos:
            continue
        d = ((float(pos["x"]) - x) ** 2 + (float(pos["z"]) - z) ** 2) ** 0.5
        if d <= best_d:
            best_d = d
            best = {
                "x": float(pos["x"]),
                "z": float(pos["z"]),
                "radius": radius,
            }
    return best


def _derive_entrance(level: dict, group: str,
                     marker_points: list[dict]) -> dict | None:
    candidates = []
    for sub in level.get("subLevels") or []:
        name = str(sub.get("name") or "").strip()
        pos = sub.get("pos") or {}
        if not name or "x" not in pos or "y" not in pos:
            continue
        score = 0
        if name == group:
            score = 100
        elif group in name or name in group:
            score = 50 - abs(len(name) - len(group))
        if score:
            candidates.append((score, pos["x"], pos["y"]))
    if not candidates:
        return None
    _, x, z = max(candidates, key=lambda item: item[0])
    snapped = _nearest_entrance(marker_points, float(x), float(z))
    return snapped or {"x": float(x), "z": float(z), "radius": 300.0}


def _region_version(region_versions: dict, map_id: str,
                    level_id: str) -> str:
    return str(
        region_versions.get(level_id)
        or region_versions.get(map_id)
        or ""
    )


def fetch_entrance_markers(tree: dict, log=print) -> dict[str, list[dict]]:
    """Fetch exact layer-entrance markers, including ``targetRegionId``."""
    result: dict[str, list[dict]] = {}
    for map_info in tree.get("maps") or []:
        map_id = str(map_info.get("id") or "")
        if not map_id:
            continue
        url = (
            "https://zonai.skland.com/web/v1/game/endfield/"
            f"map/mark/list?mapId={map_id}"
        )
        try:
            payload = json.loads(_fetch_text(url))
            marks = (payload.get("data") or {}).get("marks") or []
        except Exception as e:  # noqa: BLE001
            log(f"warning: entrance markers unavailable for {map_id}: {e}")
            continue
        result[map_id] = [
            mark for mark in marks
            if mark.get("templateId") == LAYER_ENTRANCE_TID
        ]
    return result


def collect_layer_entries(tree: dict, mapping: dict, region_versions: dict,
                          old_entries: list[dict],
                          entrance_markers: dict,
                          log=print) -> tuple[list[dict], list[str]]:
    old = {entry.get("id"): entry for entry in old_entries or []}
    entries: list[dict] = []
    missing: list[str] = []
    for map_info in tree.get("maps") or []:
        map_id = str(map_info.get("id") or "")
        map_name = MAP_DISPLAY_NAMES.get(
            map_id, str(map_info.get("name") or map_id)
        )
        for level in map_info.get("levels") or []:
            level_id = str(level.get("id") or "")
            level_name = str(level.get("name") or level_id)
            points = entrance_markers.get(map_id) or []
            has_marker_data = map_id in entrance_markers
            for set_index, region_set in enumerate(level.get("regionSets") or []):
                regions = region_set.get("regions") or []
                set_id = str(region_set.get("id") or set_index + 1)
                group = _group_name(level_name, regions, set_id)
                group_key = f"{level_id}:{set_id}"
                exact_entrances = []
                for marker in points:
                    if str(marker.get("levelId") or "") != level_id:
                        continue
                    if str(marker.get("targetRegionId") or "") != set_id:
                        continue
                    pos = marker.get("pos") or {}
                    if "x" in pos and "z" in pos:
                        exact_entrances.append({
                            "x": float(pos["x"]),
                            "z": float(pos["z"]),
                            "radius": 28.0,
                        })
                old_group = next(
                    (
                        entry for entry in old.values()
                        if entry.get("group_key") == group_key
                    ),
                    {},
                )
                if exact_entrances:
                    entrances = exact_entrances
                elif has_marker_data:
                    # The official marker list is authoritative.  A missing
                    # targetRegionId means this is an independent scene, not
                    # a layered group with a guessed entrance.
                    entrances = []
                else:
                    entrances = (
                        old_group.get("entrances")
                        or ([old_group["entrance"]]
                            if old_group.get("entrance") else [])
                    )
                if not entrances and not has_marker_data:
                    derived = _derive_entrance(level, group, points)
                    if derived:
                        entrances = [derived]
                if not entrances:
                    group = INDEPENDENT_MAP_NAMES.get(
                        level_id, level_name or group
                    )
                for region_index, region in enumerate(regions):
                    logic_id = str(region.get("logicId") or "")
                    config = mapping.get(logic_id)
                    if not logic_id or not config:
                        if logic_id:
                            missing.append(logic_id)
                        continue
                    old_entry = old.get(logic_id) or {}
                    bounds_raw = config.get("bounds") or []
                    if len(bounds_raw) != 2:
                        missing.append(logic_id)
                        continue
                    west = float(bounds_raw[0]["lng"])
                    south = float(bounds_raw[0]["lat"])
                    east = float(bounds_raw[1]["lng"])
                    north = float(bounds_raw[1]["lat"])
                    version = _region_version(
                        region_versions, map_id, level_id
                    )
                    icon = str(config.get("icon") or "")
                    region_segment = f"region_{version}" if version else "region"
                    filename = f"{logic_id}.png"
                    entries.append({
                        "id": logic_id,
                        "map_name": map_id,
                        "map_display_name": map_name,
                        "map_category": (
                            "layered" if entrances else "independent"
                        ),
                        "level_id": level_id,
                        "level_name": level_name,
                        "region_set_id": set_id,
                        "region_id": region.get("id"),
                        "group": (
                            group if not entrances
                            else old_entry.get("group") or group
                        ),
                        "group_key": old_entry.get("group_key") or group_key,
                        "name": str(region.get("name") or logic_id),
                        "tier_index": int(region.get("tierIndex") or 0),
                        "logic_id": logic_id,
                        "icon": icon,
                        "filename": filename,
                        "path": f"{map_id}/{level_id}/{filename}",
                        "source_url": (
                            ASSET_BASE + region_segment + "/" + icon + ".png"
                        ),
                        "region_version": version,
                        "bounds": {
                            "west": west,
                            "east": east,
                            "south": south,
                            "north": north,
                        },
                        "entrance": entrances[0] if entrances else {},
                        "entrances": entrances,
                        "sort_order": set_index * 100 + region_index,
                    })
    entries.sort(key=lambda item: (
        item["map_name"],
        item["level_id"],
        item["sort_order"],
        item["logic_id"],
    ))
    return entries, sorted(set(missing))


def audit_entrance_markers(entrance_markers: dict,
                           entries: list[dict]) -> dict:
    groups = {
        (entry.get("level_id"), str(entry.get("region_set_id")))
        for entry in entries
    }
    matched = 0
    unmatched = []
    for map_id, marks in entrance_markers.items():
        for mark in marks:
            key = (
                str(mark.get("levelId") or ""),
                str(mark.get("targetRegionId") or ""),
            )
            if key in groups:
                matched += 1
            else:
                pos = mark.get("pos") or {}
                unmatched.append({
                    "map_id": map_id,
                    "level_id": key[0],
                    "target_region_id": key[1],
                    "x": pos.get("x"),
                    "z": pos.get("z"),
                })
    return {"matched": matched, "unmatched": unmatched}


def _catalog_image_size(path: Path) -> tuple[int, int] | None:
    if Image is None:
        return None
    try:
        with Image.open(path) as img:
            return int(img.width), int(img.height)
    except Exception:
        return None


def download_layer_images(entries: list[dict], out_dir: Path, force: bool,
                          workers: int, log=print) -> dict:
    stats = {"downloaded": 0, "cached": 0, "errors": 0}

    def fetch_one(entry: dict):
        dest = out_dir / entry["path"]
        if dest.is_file() and not force:
            size = _catalog_image_size(dest)
            if size:
                entry["width"], entry["height"] = size
                return "cached", None
        try:
            # 与服务端按需缓存用同一个上限：CLI 抓得下来的图，编辑器也必须承认，
            # 否则缓存目录里会出现「预抓了但编辑器说下载失败」的半成品。
            data = _fetch_bytes(entry["source_url"],
                                max_bytes=MAX_LAYER_IMAGE_BYTES)
            if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                raise OSError("not a PNG")
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".part")
            tmp.write_bytes(data)
            tmp.replace(dest)
            size = _catalog_image_size(dest)
            if Image is not None and not size:
                raise OSError("invalid PNG")
            if size:
                entry["width"], entry["height"] = size
            return "downloaded", None
        except Exception as e:  # noqa: BLE001
            return "error", f"{entry['id']}: {e}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch_one, entry) for entry in entries]
        for future in concurrent.futures.as_completed(futures):
            kind, error = future.result()
            stats[
                "downloaded" if kind == "downloaded"
                else "cached" if kind == "cached"
                else "errors"
            ] += 1
            if error:
                log(f"warning: {error}")
    return stats


def build_parser(prog: str = "nav-grid-editor fetch-layers") -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog=prog,
        description="抓取官方地图全部分层 PNG，并生成 catalog.json",
    )
    ap.add_argument("--out", default=None,
                    help="输出目录（默认 <仓库>/assets/tiles/layers）")
    ap.add_argument("--force", action="store_true", help="重新下载已有图片")
    ap.add_argument("--dry-run", action="store_true",
                    help="只生成目录，不下载图片")
    ap.add_argument("--workers", type=int, default=8,
                    help="并发下载数，默认 8")
    ap.add_argument("--bundle-cache", default=None,
                    help="官方 bundle 缓存目录（默认 <out>/.bundle-cache）")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out_dir = (
        Path(args.out).resolve()
        if args.out
        else PROJECT_ROOT / "assets" / "tiles" / "layers"
    )
    cache_dir = (
        Path(args.bundle_cache).resolve()
        if args.bundle_cache
        else out_dir / ".bundle-cache"
    )
    old_catalog = _load_json(out_dir / "catalog.json") or {}
    old_entries = old_catalog.get("layers") if isinstance(old_catalog, dict) else []
    print(f"读取官方地图树: {TREE_URL}")
    tree = json.loads(_fetch_text(TREE_URL))["data"]
    print("读取官方分层入口标记…")
    entrance_markers = fetch_entrance_markers(tree, print)
    marker_count = sum(len(items) for items in entrance_markers.values())
    print(f"分层入口标记 {marker_count} 个")
    print(f"发现地图 {len(tree.get('maps') or [])} 个，正在解析前端 bundle…")
    mapping, region_versions, source_info = _discover_bundle(cache_dir, print)
    entries, missing = collect_layer_entries(
        tree, mapping, region_versions, old_entries, entrance_markers, print
    )
    entrance_audit = audit_entrance_markers(entrance_markers, entries)
    print(
        f"官方 region 逻辑 {len(mapping)} 个，可下载分层 {len(entries)} 个，"
        f"缺少资源映射 {len(missing)} 个；"
        f"入口映射 {entrance_audit['matched']}/"
        f"{entrance_audit['matched'] + len(entrance_audit['unmatched'])}"
    )

    stats = {"downloaded": 0, "cached": 0, "errors": 0}
    if args.dry_run:
        for entry in entries:
            size = _catalog_image_size(out_dir / entry["path"])
            if size:
                entry["width"], entry["height"] = size
    else:
        stats = download_layer_images(
            entries, out_dir, args.force, max(1, args.workers), print
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    catalog = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source_info,
        "region_versions": region_versions,
        "missing_logic_ids": missing,
        "entrance_audit": entrance_audit,
        "layers": entries,
    }
    catalog_path = out_dir / "catalog.json"
    tmp = catalog_path.with_name(catalog_path.name + ".part")
    tmp.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(catalog_path)

    print()
    print("=" * 60)
    print(f"LAYERS           : {len(entries)}")
    print(f"DOWNLOADED       : {stats['downloaded']}")
    print(f"CACHED           : {stats['cached']}")
    print(f"ERRORS           : {stats['errors']}")
    print(f"CATALOG          : {catalog_path}")
    if missing:
        print(f"MISSING MAPPING  : {', '.join(missing[:10])}")
    print("=" * 60)
    return 1 if stats["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
