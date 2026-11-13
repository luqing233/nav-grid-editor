"""Export v3 native tiles from a v2 bundle, preserving fine-match pixels exactly."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import cv2
import numpy as np

from .dataset_export import _json
from .localization_tiles import _Bundle, _decode, _lossless_webp, _match_color
from .localization_search_compaction import _nonzero_roi, _publish_compact, _read_verified, _write_release


def _tile_pixels(raw, layer):
    if layer:
        return _match_color(_decode(raw))
    return _decode(raw, cv2.IMREAD_COLOR)


def _compact_tile(source, target, record, layer):
    raw = _read_verified(source, record)
    image = _tile_pixels(raw, layer)
    width, height = record["width"], record["height"]
    if image.shape != (height, width, 3) or image.dtype != np.uint8:
        raise ValueError(f"瓦片尺寸或像素格式错误: {record['path']}")
    roi = _nonzero_roi(image)
    left, top, right, bottom = roi
    if layer:
        decoded = _decode(raw)
        alpha = decoded[:, :, 3] if decoded.shape[2] == 4 else np.full(decoded.shape[:2], 255, np.uint8)
        if np.any(alpha):
            # Matching BGR does not encode opaque black or partial coverage.
            # Keep the native payload so compositing can recover all alpha.
            name = record["path"]
            payload = target.files.get(name) or target.write(name, raw)
            return {"storage": "full", "roi": roi, **payload}
    if roi == [0, 0, 0, 0]:
        if np.any(image):
            raise ValueError(f"零块校验失败: {record['path']}")
        return {"storage": "zero", "roi": roi, "path": None, "size": 0, "sha256": None}

    if roi != [0, 0, width, height]:
        cropped = image[top:bottom, left:right]
        encoded = _lossless_webp(cropped, record["path"])
        if len(encoded) < len(raw):
            restored = np.zeros_like(image)
            restored[top:bottom, left:right] = _decode(encoded, cv2.IMREAD_COLOR)
            if not np.array_equal(restored, image):
                raise ValueError(f"ROI 补零后瓦片像素不一致: {record['path']}")
            return {"storage": "roi", "roi": roi, **target.tile_object(encoded)}

    name = record["path"]
    payload = target.files.get(name) or target.write(name, raw)
    return {"storage": "full", "roi": roi, **payload}


def _build(source, stage):
    dataset = _json(source / "dataset.json")
    manifest_record = dataset["localization_manifest"]
    manifest = json.loads(_read_verified(source, manifest_record))
    if manifest_record["path"] != "localization/manifest.json" or manifest["schema_version"] != 2:
        raise ValueError("原生瓦片裁边输入须为定位瓦片包 v2")
    local = source / "localization"
    files = {record["path"]: record for record in manifest["files"]}
    catalog = json.loads(_read_verified(local, files[manifest["layer_catalog"]]))
    paths = [(row["image"], False) for row in manifest["base_maps"]]
    paths += [(f"{manifest['layers_root']}/{row['path']}", True) for row in catalog["layers"]]
    bundle = _Bundle(stage / "localization")
    replaced = {path for path, _ in paths}
    source_objects, target_objects = set(), set()
    variants, reports = {}, []
    total = Counter()
    for path, layer in paths:
        index = json.loads(_read_verified(local, files[path]))
        index["schema_version"] = 3
        counts = Counter(zero=0, roi=0, full=0, full_with_zero_border=0)
        pixels_before = pixels_after = 0
        for tile in index["tiles"]:
            original = tile["path"]
            source_objects.add(original)
            width, height = tile["width"], tile["height"]
            key = (original, layer, width, height)
            if key not in variants:
                variants[key] = _compact_tile(local, bundle, tile, layer)
            tile.update(variants[key])
            storage = tile["storage"]
            counts[storage] += 1
            pixels_before += width * height
            if storage == "full":
                pixels_after += width * height
                counts["full_with_zero_border"] += tile["roi"] != [0, 0, width, height]
            elif storage == "roi":
                left, top, right, bottom = tile["roi"]
                pixels_after += (right-left) * (bottom-top)
            if tile["path"] is not None:
                target_objects.add(tile["path"])
        total.update(counts)
        # Dense arrays of tile records are machine-read. Keep index byte costs separate from object bytes.
        raw = (json.dumps(index, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode()
        result = bundle.write(path, raw)
        reports.append({"id": index["id"], "map_id": index["map_id"], "path": path,
                        "counts": dict(counts), "decoded_pixels_before": pixels_before,
                        "decoded_pixels_after": pixels_after, "index_bytes_before": files[path]["size"],
                        "index_bytes_after": result["size"], "pixel_equal": True})

    replaced.update(source_objects)
    for name, record in files.items():
        if name not in replaced:
            bundle.write(name, _read_verified(local, record))
    before = (source / "dataset.json").stat().st_size + manifest_record["size"]
    before += sum(record["size"] for record in (*dataset["navigation_files"], *files.values()))
    manifest["schema_version"] = 3
    manifest["tile_layout"] = "zero-padded-roi"
    after = _write_release(source, stage, dataset, manifest, bundle)
    return {
        "source_release_id": dataset["parent_release_id"], "release_id": dataset["release_id"],
        "package_bytes_before": before, "package_bytes_after": after,
        "objects_before": len(source_objects), "objects_after": len(target_objects),
        "object_bytes_before": sum(files[name]["size"] for name in source_objects),
        "object_bytes_after": sum(bundle.files[name]["size"] for name in target_objects),
        "unique_decode_variants_verified": len(variants), "counts": dict(total),
        **{key: sum(row[key] for row in reports) for key in (
            "decoded_pixels_before", "decoded_pixels_after", "index_bytes_before", "index_bytes_after"
        )},
        "images": reports,
    }


def compact_localization_native_tiles(source, output):
    return _publish_compact(source, output, _build)
