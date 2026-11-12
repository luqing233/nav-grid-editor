"""Export v2 search rasters by losslessly trimming zero borders from a v1 bundle."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from uuid import uuid4

import cv2
import numpy as np

from .dataset_export import _json, _record, _relative, _write_json
from .localization_tiles import _Bundle, _decode, verify_localization_tiles


def _read_verified(root, record):
    raw = _relative(root, record["path"]).read_bytes()
    if len(raw) != record["size"] or hashlib.sha256(raw).hexdigest() != record["sha256"]:
        raise ValueError(f"源文件大小或摘要不匹配: {record['path']}")
    return raw


def _nonzero_roi(image):
    left, top, width, height = cv2.boundingRect(np.bitwise_or.reduce(image, axis=2))
    return [left, top, left + width, top + height]


def _compact_image(source, target, record):
    raw = _read_verified(source, record)
    image = _decode(raw)
    width, height = record["width"], record["height"]
    if image.shape != (height, width, 3) or image.dtype != np.uint8:
        raise ValueError(f"搜索图必须为原尺寸 8 位 BGR: {record['path']}")
    roi = _nonzero_roi(image)
    left, top, right, bottom = roi
    w, h = right - left, bottom - top
    if w == 0:
        payload = {"path": None, "size": 0, "sha256": None}
        restored = np.zeros_like(image)
    elif (w, h) == (width, height):
        payload = target.write(record["path"], raw)
        restored = image  # Identical encoded bytes, with an identity ROI.
    else:
        payload = target.index_image(record["path"], image[top:bottom, left:right])
        restored = np.zeros_like(image)
        restored[top:bottom, left:right] = _decode(_read_verified(target.root, payload))
    if not np.array_equal(restored, image):
        raise ValueError(f"ROI 补零后与 v1 像素不一致: {record['path']}")
    return {**record, **payload, "width": width, "height": height, "roi": roi, "fill": [0, 0, 0]}


def _build(source, stage):
    dataset = _json(source / "dataset.json")
    manifest_record = dataset["localization_manifest"]
    manifest = json.loads(_read_verified(source, manifest_record))
    if manifest_record["path"] != "localization/manifest.json" or manifest["schema_version"] != 1:
        raise ValueError("裁边输入须为定位瓦片包 v1")
    local = source / "localization"
    files = {record["path"]: record for record in manifest["files"]}
    catalog = json.loads(_read_verified(local, files[manifest["layer_catalog"]]))
    index_paths = [row["image"] for row in manifest["base_maps"]]
    index_paths += [f"{manifest['layers_root']}/{row['path']}" for row in catalog["layers"]]
    bundle = _Bundle(stage / "localization")
    replaced = set(index_paths)
    reports = []

    def compact(record, role, image_id, map_id):
        replaced.add(record["path"])
        updated = _compact_image(local, bundle, record)
        left, top, right, bottom = updated["roi"]
        reports.append({"id": image_id, "map_id": map_id, "role": role, "source_path": record["path"],
                        "screen_width": record.get("screen_width"), "width": record["width"],
                        "height": record["height"], "roi": updated["roi"],
                        "pixels_before": record["width"] * record["height"],
                        "pixels_after": (right-left) * (bottom-top), "bytes_before": record["size"],
                        "bytes_after": updated["size"], "pixel_equal": True})
        return updated

    for path in index_paths:
        index = json.loads(_read_verified(local, files[path]))
        index["schema_version"] = 2
        index["coarse"] = [compact(row, "coarse", index["id"], index["map_id"]) for row in index["coarse"]]
        if index["floor_rank"] is not None:
            index["floor_rank"] = compact(index["floor_rank"], "floor_rank", index["id"], index["map_id"])
        bundle.json(path, index)

    for name, record in files.items():
        if name not in replaced:
            bundle.write(name, _read_verified(local, record))
    before = (source / "dataset.json").stat().st_size + manifest_record["size"]
    before += sum(record["size"] for record in (*dataset["navigation_files"], *files.values()))
    manifest["schema_version"] = 2
    manifest["search"]["image_layout"] = "zero-padded-roi"
    after = _write_release(source, stage, dataset, manifest, bundle)

    summary = {}
    for role in ("coarse", "floor_rank"):
        rows = [row for row in reports if row["role"] == role]
        summary[role] = {key: sum(row[key] for row in rows) for key in
                         ("pixels_before", "pixels_after", "bytes_before", "bytes_after")}
        summary[role].update(count=len(rows), all_black=sum(row["pixels_after"] == 0 for row in rows))
    return {"source_release_id": dataset["parent_release_id"], "release_id": dataset["release_id"],
            "package_bytes_before": before,
            "package_bytes_after": after,
            "summary": summary, "images": reports}


def _write_release(source, stage, dataset, manifest, bundle):
    for record in dataset["navigation_files"]:
        path = _relative(stage, record["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_read_verified(source, record))
    manifest["files"] = sorted(bundle.files.values(), key=lambda record: record["path"])
    bundle.json("manifest.json", manifest)
    dataset["parent_release_id"] = dataset["release_id"]
    dataset["release_id"] = str(uuid4())
    dataset["localization_manifest"] = _record(stage, stage / "localization/manifest.json")
    _write_json(stage / "dataset.json", dataset)
    verify_localization_tiles(stage)
    return (stage / "dataset.json").stat().st_size + sum(
        record["size"] for record in (*dataset["navigation_files"], *bundle.files.values())
    )


def _publish_compact(source, output, build):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists() or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("输出须为与输入独立的新目录")
    verify_localization_tiles(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".compact-localization-", dir=output.parent) as temporary:
        staging = Path(temporary).resolve()
        if staging.parent != output.parent:
            raise ValueError("暂存目录超出输出父目录")
        stage = staging / "bundle"
        report = build(source, stage)
        _write_json(stage / "validation.json", report)
        os.rename(stage, output)
    return report


def compact_localization_tiles(source, output):
    return _publish_compact(source, output, _build)
