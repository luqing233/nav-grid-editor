"""Export a standalone localization dataset from full Q90 presets and original floor PNGs."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import cv2
import numpy as np
from PIL import Image

from .dataset_export import _calibration, _digest, _json, _record, _relative, _write_json, verify_dataset
from .io import save_lossless_webp, save_webp

SCREEN_WIDTHS = (1280, 1600, 1920, 2560, 3440, 3840)
SCALE_CONSTANT = 1706.5
MATCH_PIXELS_PER_METER = 4.0
COARSE_FACTOR = 0.5
FLOOR_TILE_SIZE = 512


def _decode(raw, flags=cv2.IMREAD_UNCHANGED):
    decoded = cv2.imdecode(np.frombuffer(raw, np.uint8), flags)
    if decoded is None:
        raise ValueError("无法解码定位图片")
    return decoded


def _match_color(image):
    if image.ndim == 2:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    if image.shape[2] == 3:
        return image
    color = image[:, :, :3].copy()
    color[image[:, :, 3] == 0] = 0
    return color


def _lossless_webp(image, label):
    ok, encoded = cv2.imencode(".webp", image, [cv2.IMWRITE_WEBP_QUALITY, 101])
    if not ok:
        raise ValueError(f"无损 WebP 编码失败: {label}")
    raw = encoded.tobytes()
    if not np.array_equal(_decode(raw), image):
        raise ValueError(f"无损 WebP 校验失败: {label}")
    return raw


def _positions(width, height, edge):
    for y in range(0, height, edge):
        for x in range(0, width, edge):
            yield x, y, min(edge, width - x), min(edge, height - y)


def _seam_report(image, reference, edge):
    """Compare decoded pixels and the change in RGB gradients across tile boundaries."""
    image, reference = _match_color(image), _match_color(reference)
    if image.shape != reference.shape:
        raise ValueError("对照整图尺寸不一致")
    errors = []
    for axis in (0, 1):
        tiled = np.swapaxes(image, 0, axis)
        old = np.swapaxes(reference, 0, axis)
        for i in range(edge, tiled.shape[0], edge):
            new_jump = tiled[i, ::8].astype(np.int16) - tiled[i - 1, ::8]
            old_jump = old[i, ::8].astype(np.int16) - old[i - 1, ::8]
            errors.append(np.abs(new_jump - old_jump).ravel())
    seam = np.concatenate(errors) if errors else np.zeros(1)
    sampled = np.abs(image[::8, ::8].astype(np.int16) - reference[::8, ::8])
    return {"sample_step": 8, "pixel_mae": float(sampled.mean()),
            "seam_gradient_mae": float(seam.mean()), "seam_gradient_p99": float(np.quantile(seam, 0.99)),
            "seam_gradient_max": int(seam.max())}


class _Bundle:
    def __init__(self, root):
        self.root = root
        self.files = {}

    def write(self, name, raw):
        path = _relative(self.root, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        record = {"path": name, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        self.files[name] = record
        return record

    def json(self, name, data):
        return self.write(name, (json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode())

    def tile_object(self, raw):
        digest = hashlib.sha256(raw).hexdigest()
        name = f"objects/{digest[:2]}/{digest}.webp"
        return self.files.get(name) or self.write(name, raw)

    def tile(self, raw, rect, edge):
        x, y, width, height = rect
        return {**self.tile_object(raw), "column": x // edge, "row": y // edge, "width": width, "height": height}

    def index_image(self, name, image):
        raw = _lossless_webp(image, name)
        return {**self.write(name, raw), "width": image.shape[1], "height": image.shape[0]}

    def search_images(self, directory, image, bounds=None):
        color = _match_color(image)
        fx = fy = 1.0
        proposal = None
        if bounds is not None:
            dx, dz = bounds["east"] - bounds["west"], bounds["north"] - bounds["south"]
            width, height = image.shape[1], image.shape[0]
            fx, fy = 4 * (dx / width), 4 * (dz / height)
            size = max(1, round(dx * 0.5)), max(1, round(dz * 0.5))
            # The floor ranker resizes RGBA first and masks fully transparent output pixels.
            reduced = cv2.resize(image, size, interpolation=cv2.INTER_AREA)
            proposal = self.index_image(f"{directory}/rank.webp", _match_color(reduced))
            color = cv2.resize(color, None, fx=fx, fy=fy, interpolation=cv2.INTER_AREA)
        normalized = {"width": color.shape[1], "height": color.shape[0], "source_scale": [fx, fy]}
        overviews = []
        for width in SCREEN_WIDTHS:
            scale = width / (MATCH_PIXELS_PER_METER * SCALE_CONSTANT) * COARSE_FACTOR
            reduced = cv2.resize(color, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            overviews.append({**self.index_image(f"{directory}/coarse-{width}.webp", reduced),
                              "screen_width": width, "scale": scale, "scale_hex": scale.hex()})
        return {"match_raster": normalized, "coarse": overviews, "floor_rank": proposal}


def _base_tiles(bundle, preset_root, records, base, layout, edge):
    width, height = base["width"], base["height"]
    columns = layout["maxX"] - layout["minX"] + 1
    rows = layout["maxY"] - layout["minY"] + 1
    if (width, height) != (columns * edge, rows * edge) or layout["count"] != columns * rows:
        raise ValueError(f"预设边界与标定尺寸不一致: {base['map_id']}")
    image = np.empty((height, width, 3), np.uint8)
    tiles = []
    for rect in _positions(width, height, edge):
        x, y, w, h = rect
        tile_x, tile_y = layout["minX"] + x // edge, layout["minY"] + y // edge
        name = f"{base['map_id']}/{base['zoom']}/{tile_x}_{tile_y}.webp"
        raw = _relative(preset_root, name).read_bytes()
        record = records[name]
        if len(raw) != record["size"] or hashlib.sha256(raw).hexdigest() != record["sha256"]:
            raise ValueError(f"预设瓦片大小或摘要不匹配: {name}")
        if raw[:4] != b"RIFF" or raw[8:12] != b"WEBP":
            raise ValueError(f"预设瓦片不是 WebP: {name}")
        decoded = _decode(raw, cv2.IMREAD_COLOR)
        if decoded.shape != (h, w, 3):
            raise ValueError(f"预设瓦片尺寸不匹配: {name}")
        image[y:y+h, x:x+w] = decoded
        tiles.append(bundle.tile(raw, rect, edge))
    return image, tiles


def _floor_tiles(bundle, path, width, height, executor, *, allow_webp=False):
    edge = FLOOR_TILE_SIZE
    raw = path.read_bytes()
    with Image.open(BytesIO(raw)) as source:
        allowed = ("PNG", "WEBP") if allow_webp else ("PNG",)
        if source.format not in allowed or source.size != (width, height):
            raise ValueError(f"原始楼层图片格式或尺寸不匹配: {path}")
        preserve_pixels = source.format == "WEBP"
        transparent = "A" in source.getbands() or "transparency" in source.info
        original = source.convert("RGBA" if transparent else "RGB")
    with original:
        image = np.empty((height, width, 4 if transparent else 3), np.uint8)

        def encode(rect):
            x, y, w, h = rect
            with original.crop((x, y, x+w, y+h)) as tile, BytesIO() as stream:
                if preserve_pixels:
                    save_lossless_webp(tile, stream)
                else:
                    save_webp(tile, stream)
                payload = stream.getvalue()
                decoded = _decode(payload)
                if transparent:
                    if decoded.shape == (h, w, 3):
                        decoded = cv2.cvtColor(decoded, cv2.COLOR_BGR2BGRA)
                    if not np.array_equal(decoded[:, :, 3], np.asarray(tile.getchannel("A"))):
                        raise ValueError(f"楼层瓦片 alpha 校验失败: {path} {x},{y}")
                if preserve_pixels and not np.array_equal(decoded[:, :, :3], np.asarray(tile)[:, :, :3][:, :, ::-1]):
                    raise ValueError(f"楼层瓦片无损切块校验失败: {path} {x},{y}")
                image[y:y+h, x:x+w] = decoded
            return rect, payload

        tiles = [bundle.tile(payload, rect, edge) for rect, payload in
                 executor.map(encode, _positions(width, height, edge))]
    source_kind = "existing-floor-webp-lossless-tiles" if preserve_pixels else "original-floor-png"
    return image, tiles, {"kind": source_kind, "sha256": hashlib.sha256(raw).hexdigest()}


def _build(presets, snapshot, originals, stage, compare, *, floor_sources=None, progress=None):
    preset = _json(presets / "manifest.json")
    if (preset["schema_version"], preset["kind"], preset["encoding"]) != (
        1, "nav-grid-editor-tile-presets", {"format": "webp", "quality": 90, "method": 4}
    ) or preset["tile_size"] != 256:
        raise ValueError("需要 v1、256px 的 Q90 预设")
    records = {row["path"]: row for row in preset["files"]}
    layouts = {(row["map"], row["zoom"]): row for row in preset["maps"]}
    if len(records) != len(preset["files"]) or len(layouts) != len(preset["maps"]):
        raise ValueError("预设清单存在重复路径或地图")
    dataset = _json(snapshot / "dataset.json")
    legacy_record = dataset["localization_manifest"]
    legacy_path = _relative(snapshot, legacy_record["path"])
    if _record(snapshot, legacy_path) != legacy_record:
        raise ValueError("源定位清单摘要不符")
    legacy = _json(legacy_path)
    kind = "okef-visual-localization-input" if floor_sources is not None else "okef-visual-localization"
    if (legacy["schema_version"], legacy["kind"]) != (1, kind):
        raise ValueError("需要 v1 整图数据包作为标定和导航元数据来源")
    legacy_root = legacy_path.parent
    legacy_files = {r["path"]: r for r in legacy["files"]}

    def read_legacy(name):
        path = _relative(legacy_root, name)
        raw = path.read_bytes()
        if len(raw) != legacy_files[name]["size"] or hashlib.sha256(raw).hexdigest() != legacy_files[name]["sha256"]:
            raise ValueError(f"源资源摘要不符: {name}")
        return raw

    catalog = json.loads(read_legacy(legacy["layer_catalog"]))
    bundle = _Bundle(stage / "localization")
    references, base_maps, comparison = {}, [], {}
    edge = preset["tile_size"]
    if floor_sources is not None:
        keys = {(row["map_id"], row["zoom"]) for row in legacy["base_maps"]}
        if keys != layouts.keys():
            raise ValueError("预设地图与已标定主图不一致")
        expected_paths = {
            f"{mid}/{zoom}/{x}_{y}.webp" for (mid, zoom), layout in layouts.items()
            for y in range(layout["minY"], layout["maxY"] + 1)
            for x in range(layout["minX"], layout["maxX"] + 1)
        }
        if expected_paths != records.keys():
            raise ValueError("预设瓦片清单有缺口或孤立记录")
    for base in legacy["base_maps"]:
        mid, zoom = base["map_id"], base["zoom"]
        directory = f"maps/{mid}/{zoom}"
        image, tiles = _base_tiles(bundle, presets, records, base, layouts[mid, zoom], edge)
        mapping = read_legacy(base["mapping"])
        _calibration(json.loads(mapping), (base["width"], base["height"]))
        bundle.write(base["mapping"], mapping)
        index = f"{directory}/tiles.json"
        bundle.json(index, {"schema_version": 1, "kind": "okef-image-tiles", "id": mid, "map_id": mid,
                            "zoom": zoom, "width": base["width"], "height": base["height"],
                            "tile_size": edge, "complete": True, "tiles": tiles,
                            "source": {"kind": "q90-composite-derived-preset",
                                       "tile_origin": [layouts[mid, zoom]["minX"], layouts[mid, zoom]["minY"]]},
                            "mapping": base["mapping"], **bundle.search_images(directory, image)})
        if compare:
            comparison[f"base:{mid}"] = _seam_report(image, _decode(read_legacy(base["image"])), edge)
        base_maps.append({**base, "image": index})
        references[f"localization/{base['image']}"] = f"localization/{index}"
        print(f"基础图 {mid}: {len(tiles)} 张，复用原字节", flush=True)

    with ThreadPoolExecutor(max_workers=6) as executor:
        for number, layer in enumerate(catalog["layers"], 1):
            old_path = layer["path"]
            original = Path(old_path).with_suffix(".png").as_posix()
            path = _relative(originals, original) if floor_sources is None else floor_sources[old_path]
            width, height = layer["width"], layer["height"]
            image, tiles, source_record = _floor_tiles(bundle, path, width, height, executor,
                                                       allow_webp=floor_sources is not None)
            relative = Path(old_path).with_suffix("").as_posix()
            directory = f"{legacy['layers_root']}/{relative}"
            index = f"{directory}/tiles.json"
            bounds = layer["bounds"]
            bundle.json(index, {"schema_version": 1, "kind": "okef-image-tiles", "id": layer["id"],
                                "map_id": layer["map_name"], "level_id": layer["level_id"], "zoom": "4",
                                "map_category": layer["map_category"], "bounds": bounds,
                                "width": width, "height": height, "tile_size": FLOOR_TILE_SIZE,
                                "complete": True, "tiles": tiles,
                                "source": source_record,
                                **bundle.search_images(directory, image, bounds)})
            old_image = f"{legacy['layers_root']}/{old_path}"
            if compare:
                comparison[f"layer:{layer['id']}"] = _seam_report(image, _decode(read_legacy(old_image)), FLOOR_TILE_SIZE)
            references[f"localization/{old_image}"] = f"localization/{index}"
            layer["path"] = f"{relative}/tiles.json"
            layer["filename"] = "tiles.json"
            print(f"楼层 {number}/{len(catalog['layers'])}: {layer['id']}", flush=True)
            if progress:
                progress("生成楼层瓦片和索引", number, len(catalog["layers"]))

    bundle.json(legacy["layer_catalog"], catalog)
    manifest = {**legacy, "kind": "okef-tiled-localization", "base_maps": base_maps,
                "provenance": {"producer": "nav-grid-editor", "processing": "Q90 native tiles; lossless search indexes"},
                "raster_convention": {"origin": "top-left", "pixel_coordinates": "integer-centers",
                                      "columns": "right", "rows": "down", "index_base": 0},
                "search": {"screen_widths": list(SCREEN_WIDTHS), "scale_constant": SCALE_CONSTANT,
                           "match_pixels_per_meter": MATCH_PIXELS_PER_METER, "coarse_factor": COARSE_FACTOR,
                           "floor_rank_pixels_per_meter": 0.5, "interpolation": "OpenCV INTER_AREA"},
                "files": sorted(bundle.files.values(), key=lambda row: row["path"])}
    bundle.json("manifest.json", manifest)
    for record in dataset["navigation_files"]:
        source = _relative(snapshot, record["path"])
        target = _relative(stage, record["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    for registration in dataset["registrations"]:
        visual = registration["visual"]
        if visual is not None:
            visual["image"] = references[visual["image"]]
    dataset["parent_release_id"] = dataset["release_id"]
    dataset["release_id"] = str(uuid4())
    dataset["localization_manifest"] = _record(stage, stage / "localization/manifest.json")
    _write_json(stage / "dataset.json", dataset)
    verify_localization_tiles(stage)
    if compare:
        _write_json(stage / "comparison.json", comparison)


def _roi_payload_size(record):
    width, height = record["width"], record["height"]
    roi = record["roi"]
    if not isinstance(roi, list) or len(roi) != 4 or any(type(value) is not int for value in roi):
        raise ValueError("无效的 ROI")
    if roi == [0, 0, 0, 0]:
        if (record["path"], record["size"], record["sha256"]) != (None, 0, None):
            raise ValueError("全黑 ROI 不能引用文件")
        return None
    left, top, right, bottom = roi
    if not (0 <= left < right <= width and 0 <= top < bottom <= height) or record["path"] is None:
        raise ValueError("ROI 越界或缺少文件")
    return right - left, bottom - top


def _search_payload_size(record, version):
    if version == 1:
        return record["width"], record["height"]
    if record["fill"] != [0, 0, 0]:
        raise ValueError("无效的搜索图填充值")
    return _roi_payload_size(record)


def _tile_payload_size(record, version):
    full = record["width"], record["height"]
    if version < 3:
        return full
    size = _roi_payload_size(record)
    storage = record["storage"]
    if storage == "zero" and size is None:
        return None
    if storage == "full" and size is not None:
        return full
    if storage == "roi" and size is not None and size != full:
        return size
    raise ValueError("瓦片存储类型与 ROI 不一致")


def verify_localization_tiles(root):
    """Check payload digests, image coverage and search-index references."""
    root = Path(root).resolve()
    verify_dataset(root)
    local = root / "localization"
    manifest = _json(local / "manifest.json")
    version = manifest["schema_version"]
    if version not in (1, 2, 3) or manifest["kind"] != "okef-tiled-localization":
        raise ValueError("不支持的瓦片定位清单")
    if version >= 2 and manifest["search"].get("image_layout") != "zero-padded-roi":
        raise ValueError("不支持的搜索图布局")
    if version == 3 and manifest.get("tile_layout") != "zero-padded-roi":
        raise ValueError("不支持的原生瓦片布局")
    files = {record["path"]: record for record in manifest["files"]}
    catalog = _json(_relative(local, manifest["layer_catalog"]))
    layer_ids = {entry["id"] for entry in catalog["layers"]}
    base_ids = {(entry["map_id"], entry["zoom"]) for entry in manifest["base_maps"]}
    missing = catalog.get("missing_logic_ids", [])
    coverage = manifest["coverage"]
    if (len(layer_ids) != len(catalog["layers"]) or len(base_ids) != len(manifest["base_maps"])
            or layer_ids.intersection(missing) or coverage["missing_logic_ids"] != missing
            or coverage["layer_count"] != len(layer_ids)
            or coverage["independent_layer_count"] != sum(e["map_category"] == "independent" for e in catalog["layers"])):
        raise ValueError("定位目录的身份或覆盖声明不一致")
    present = {path.relative_to(local).as_posix() for path in local.rglob("*") if path.is_file()}
    if present != files.keys() | {"manifest.json"}:
        raise ValueError("定位目录存在孤立文件或缺少清单文件")
    used = {manifest["layer_catalog"]}
    dimensions = {}

    def check_record(record, payload_size=None):
        name = record["path"]
        if name not in files or any(record[k] != files[name][k] for k in ("size", "sha256")):
            raise ValueError(f"图片引用摘要不匹配: {name}")
        if name not in dimensions:
            with Image.open(_relative(local, name)) as image:
                if image.format != "WEBP":
                    raise ValueError(f"定位资源不是 WebP: {name}")
                dimensions[name] = image.size
        if dimensions[name] != (payload_size or (record["width"], record["height"])):
            raise ValueError(f"图片实际尺寸不匹配: {name}")
        used.add(name)

    def check_search_image(record):
        size = _search_payload_size(record, version)
        if size is not None:
            check_record(record, size)

    images = [(e["image"], e["map_id"], e) for e in manifest["base_maps"]]
    images.extend((f"{manifest['layers_root']}/{e['path']}", e["id"], e) for e in catalog["layers"])
    indexes = {}
    for name, image_id, entry in images:
        if name in used:
            raise ValueError(f"重复图片索引: {name}")
        index = _json(_relative(local, name))
        used.add(name)
        if (index["schema_version"], index["kind"], index["id"], index["complete"]) != (version, "okef-image-tiles", image_id, True):
            raise ValueError(f"不支持或不完整的图片索引: {name}")
        width, height, edge = index["width"], index["height"], index["tile_size"]
        if (width, height) != (entry["width"], entry["height"]) or min(width, height, edge) <= 0:
            raise ValueError(f"图片索引尺寸不一致: {name}")
        expected = {(x // edge, y // edge): (w, h) for x, y, w, h in _positions(width, height, edge)}
        for tile in index["tiles"]:
            if expected.pop((tile["column"], tile["row"]), None) != (tile["width"], tile["height"]):
                raise ValueError(f"重复或越界的瓦片坐标或尺寸: {name}")
            size = _tile_payload_size(tile, version)
            if size is not None:
                check_record(tile, size)
        if expected:
            raise ValueError(f"瓦片覆盖有缺口: {name}")
        if [overview["screen_width"] for overview in index["coarse"]] != list(SCREEN_WIDTHS):
            raise ValueError(f"粗搜缩图缺失: {name}")
        raster = index["match_raster"]
        for overview in index["coarse"]:
            scale = overview["screen_width"] / (MATCH_PIXELS_PER_METER * SCALE_CONSTANT) * COARSE_FACTOR
            if (overview["scale"], overview["scale_hex"], overview["width"], overview["height"]) != (
                scale, scale.hex(), round(raster["width"] * scale), round(raster["height"] * scale)
            ):
                raise ValueError(f"粗搜缩图尺寸或缩放不匹配: {name}")
            check_search_image(overview)
        if "bounds" in entry:
            if (index["bounds"] != entry["bounds"] or index["map_id"] != entry["map_name"]
                    or any(index[key] != entry[key] for key in ("level_id", "map_category"))):
                raise ValueError(f"楼层边界或归属不匹配: {name}")
            bounds = entry["bounds"]
            dx, dz = bounds["east"] - bounds["west"], bounds["north"] - bounds["south"]
            expected_scale = [4 * (dx / width), 4 * (dz / height)]
            if min(dx, dz) <= 0 or raster["source_scale"] != expected_scale:
                raise ValueError(f"楼层归一化比例不匹配: {name}")
            rank = index["floor_rank"]
            if (rank["width"], rank["height"]) != (max(1, round(dx * 0.5)), max(1, round(dz * 0.5))):
                raise ValueError(f"楼层排序图尺寸不匹配: {name}")
            check_search_image(rank)
        else:
            if index["mapping"] != entry["mapping"] or index["map_id"] != entry["map_id"]:
                raise ValueError(f"主图标定或归属不匹配: {name}")
            used.add(index["mapping"])
            _calibration(_json(_relative(local, index["mapping"])), (width, height))
            expected_scale = [1.0, 1.0]
        if raster["source_scale"] != expected_scale or (raster["width"], raster["height"]) != (
            round(width * expected_scale[0]), round(height * expected_scale[1])
        ):
            raise ValueError(f"匹配坐标系尺寸不匹配: {name}")
        indexes[f"localization/{name}"] = index
    if used != files.keys():
        raise ValueError("定位清单与瓦片索引引用不一致")
    dataset = _json(root / "dataset.json")
    registered = set()
    for registration in dataset["registrations"]:
        visual = registration["visual"]
        if visual is None:
            continue
        index = indexes[visual["image"]]
        if visual["image"] in registered:
            raise ValueError("同一定位索引重复注册")
        registered.add(visual["image"])
        if "bounds" in index:
            if visual["kind"] != "layer" or visual["catalog_entry"] != index["id"]:
                raise ValueError("楼层索引引用不符")
        elif visual["kind"] != "base" or visual["mapping"] != f"localization/{index['mapping']}":
            raise ValueError("主图标定引用不符")
        if (registration["map_name"], registration["zoom"], registration["tier"]) != (
            index["map_id"], index["zoom"], index["id"] if "bounds" in index else ""
        ):
            raise ValueError("导航注册项与瓦片索引身份不符")
    if registered != indexes.keys():
        raise ValueError("存在未注册的定位索引")


def build_localization_tiles(presets, snapshot, originals, output, *, compare=False):
    presets, snapshot, originals, output = (Path(path).resolve() for path in (presets, snapshot, originals, output))
    if output.exists() or any(output.is_relative_to(p) or p.is_relative_to(output) for p in (presets, snapshot, originals)):
        raise ValueError("输出须为与输入独立的新目录")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".localization-tiles-", dir=output.parent) as temporary:
        staging = Path(temporary).resolve()
        if staging.parent != output.parent:
            raise ValueError("暂存目录超出输出父目录")
        stage = staging / "bundle"
        _build(presets, snapshot, originals, stage, compare)
        os.rename(stage, output)
