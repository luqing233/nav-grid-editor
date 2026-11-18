"""Build and verify the runtime-ready localization competition package."""

from __future__ import annotations

import hashlib
import json
import math
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .dataset_export import _digest, _json, _read_image, _record, _relative, _write_json
from .io import save_webp
from .localization_search_compaction import _read_verified
from .localization_tiles import (
    COARSE_FACTOR,
    MATCH_PIXELS_PER_METER,
    SCALE_CONSTANT,
    _decode,
    _match_color,
)
from .settings import Image

PREPARED_DIRNAME = "prepared-v1"
PREPARED_KIND = "okef-prepared-localization"
PREPARED_VERSION = 1
INPUT_WIDTH = 1280
COMPOSITE_DIM = 0.3
COMPOSITE_PADDING = 320
ATLAS_PADDING = 128
MAX_ATLAS_WIDTH = 8192
TILE_CACHE_BYTES = 8 * 1024**2


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_ready(path: Path, digest: str) -> None:
    path.write_text(f"{PREPARED_KIND} {PREPARED_VERSION} {digest}\n", encoding="ascii", newline="\n")


def _save_q90(image: np.ndarray, target: Path) -> dict:
    if image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
        raise ValueError(f"竞争图集必须为 8 位 BGR: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    with Image.fromarray(rgb) as encoded:
        save_webp(encoded, target)
    decoded = _read_image(target, cv2.IMREAD_COLOR)
    if decoded.shape != image.shape or decoded.dtype != np.uint8:
        raise ValueError(f"WebP Q90 读回尺寸不一致: {target}")
    difference = np.abs(decoded.astype(np.int16) - image.astype(np.int16))
    return {
        "decoded_sha256": _sha256_bytes(decoded.tobytes()),
        "pixel_mae": float(difference.mean()),
        "pixel_p99": float(np.quantile(difference, 0.99)),
        "pixel_max": int(difference.max()),
    }


def _floor_matrix(entry: dict) -> np.ndarray:
    bounds = entry["bounds"]
    west, east = float(bounds["west"]), float(bounds["east"])
    south, north = float(bounds["south"]), float(bounds["north"])
    width, height = int(entry["width"]), int(entry["height"])
    if min(width, height) <= 0 or east <= west or north <= south:
        raise ValueError(f"无效楼层几何: {entry['id']}")
    sx, sy = (east - west) / width, (north - south) / height
    return np.array(
        [
            [MATCH_PIXELS_PER_METER**-1, 0, west + 0.125 - sx / 2],
            [0, -(MATCH_PIXELS_PER_METER**-1), north - 0.125 + sy / 2],
        ],
        dtype=np.float64,
    )


def _mapping_matrix(local: Path, index: dict) -> np.ndarray:
    mapping = _json(_relative(local, index["mapping"]))
    matrix = np.asarray(mapping["matrix"], dtype=np.float64)
    inverse = np.asarray(mapping["inverse_matrix"], dtype=np.float64)
    if matrix.shape != (2, 3) or inverse.shape != (2, 3):
        raise ValueError(f"主图标定矩阵形状错误: {index['mapping']}")
    forward = np.vstack((matrix, [0, 0, 1]))
    backward = np.vstack((inverse, [0, 0, 1]))
    if not np.allclose(forward @ backward, np.eye(3), atol=1e-7, rtol=0):
        raise ValueError(f"主图标定矩阵不可逆: {index['mapping']}")
    return matrix


class _TiledImage:
    """Read BGR and, for floors, alpha directly from a v3 tile index."""

    def __init__(self, local: Path, files: dict, index_path: str, *, layer: bool):
        self.local = local
        self.files = files
        self.index = _json(_relative(local, index_path))
        self.index_path = index_path
        self.layer = layer
        self.shape = (int(self.index["height"]), int(self.index["width"]), 3)
        self.edge = int(self.index["tile_size"])
        self.tiles = {(int(tile["row"]), int(tile["column"])): tile for tile in self.index["tiles"]}
        self._cache = OrderedDict()
        self._cache_bytes = 0

    def _tile(self, tile):
        key = (tile["path"], tile["storage"], tile["width"], tile["height"])
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        color = np.zeros((tile["height"], tile["width"], 3), np.uint8)
        alpha = np.zeros((tile["height"], tile["width"]), np.uint8)
        if tile["storage"] != "zero":
            raw = _read_verified(self.local, tile)
            decoded = _decode(raw)
            color_value = _match_color(decoded)
            if tile["storage"] == "full":
                if color_value.shape != color.shape:
                    raise ValueError(f"完整楼层瓦片尺寸错误: {tile['path']}")
                color[:] = color_value
                if self.layer:
                    alpha[:] = (
                        decoded[:, :, 3]
                        if decoded.ndim == 3 and decoded.shape[2] == 4
                        else np.full(decoded.shape[:2], 255, np.uint8)
                    )
            else:
                left, top, right, bottom = tile["roi"]
                color[top:bottom, left:right] = color_value
                if self.layer:
                    alpha[top:bottom, left:right] = 255
        value = (color, alpha)
        size = color.nbytes + alpha.nbytes
        self._cache[key] = value
        self._cache_bytes += size
        while self._cache_bytes > TILE_CACHE_BYTES:
            old_color, old_alpha = self._cache.popitem(last=False)[1]
            self._cache_bytes -= old_color.nbytes + old_alpha.nbytes
        return value

    def read(self, y0: int, y1: int, x0: int, x1: int, *, want_alpha=False):
        y0, y1 = max(0, int(y0)), min(self.shape[0], int(y1))
        x0, x1 = max(0, int(x0)), min(self.shape[1], int(x1))
        color = np.zeros((max(0, y1 - y0), max(0, x1 - x0), 3), np.uint8)
        alpha = np.zeros((max(0, y1 - y0), max(0, x1 - x0)), np.uint8) if want_alpha else None
        if color.size == 0:
            return color, alpha
        for row in range(y0 // self.edge, (y1 - 1) // self.edge + 1):
            for column in range(x0 // self.edge, (x1 - 1) // self.edge + 1):
                tile = self.tiles.get((row, column))
                if tile is None:
                    continue
                tile_color, tile_alpha = self._tile(tile)
                oy, ox = row * self.edge, column * self.edge
                left, top = max(x0, ox), max(y0, oy)
                right = min(x1, ox + tile["width"])
                bottom = min(y1, oy + tile["height"])
                if left >= right or top >= bottom:
                    continue
                color[top - y0 : bottom - y0, left - x0 : right - x0] = tile_color[
                    top - oy : bottom - oy, left - ox : right - ox
                ]
                if alpha is not None:
                    alpha[top - y0 : bottom - y0, left - x0 : right - x0] = tile_alpha[
                        top - oy : bottom - oy, left - ox : right - ox
                    ]
        return color, alpha


def _blend_regions(foreground, alpha, background, dim):
    result = np.empty_like(foreground)
    height, width = foreground.shape[:2]
    for y in range(0, height, 128):
        for x in range(0, width, 256):
            region = np.s_[y : y + 128, x : x + 256]
            weight = alpha[region].astype(np.float32)[:, :, None] / 255
            mixed = weight * foreground[region]
            mixed += (1 - weight) * dim * background[region]
            np.rint(mixed, out=mixed)
            result[region] = mixed
    return result


def _axis_taps(start, stop, scale, length, area):
    output = np.arange(start, stop, dtype=np.float64)
    left = output / scale
    first = np.floor(left).astype(np.intp)
    if not area:
        fraction = output + 1 - (first + 1) * scale
        fraction = np.maximum(0, fraction - np.floor(np.maximum(0, fraction)))
        return [(first, 1 - fraction), (np.minimum(first + 1, length - 1), fraction)]
    right = np.minimum((output + 1) / scale, length)
    taps = []
    for offset in range(int(np.max(np.ceil(right) - first))):
        index = first + offset
        weight = np.maximum(0, np.minimum(right, index + 1) - np.maximum(left, index)) / (right - left)
        taps.append((np.minimum(index, length - 1), weight))
    return taps


class _CompositeFloor:
    def __init__(self, foreground: _TiledImage, base: _TiledImage, entry: dict, base_matrix: np.ndarray):
        self.foreground = foreground
        self.base = base
        self.entry = entry
        self.padding = COMPOSITE_PADDING
        matrix = _floor_matrix(entry)
        self.calibration = matrix.copy()
        self.calibration[:, 2] -= matrix[:, :2] @ np.array([self.padding, self.padding])
        self._base_transform = np.vstack((base_matrix, [0, 0, 1])) @ np.linalg.inv(
            np.vstack((self.calibration, [0, 0, 1]))
        )
        self._base_transform = self._base_transform[:2]
        self.shape = (
            foreground.shape[0] + 2 * self.padding,
            foreground.shape[1] + 2 * self.padding,
            3,
        )

    def _floor_region(self, y0, y1, x0, x1):
        color = np.zeros((y1 - y0, x1 - x0, 3), np.uint8)
        alpha = np.zeros((y1 - y0, x1 - x0), np.uint8)
        padding = self.padding
        left, top = max(x0, padding), max(y0, padding)
        right = min(x1, padding + self.foreground.shape[1])
        bottom = min(y1, padding + self.foreground.shape[0])
        if left < right and top < bottom:
            source_color, source_alpha = self.foreground.read(
                top - padding,
                bottom - padding,
                left - padding,
                right - padding,
                want_alpha=True,
            )
            color[top - y0 : bottom - y0, left - x0 : right - x0] = source_color
            alpha[top - y0 : bottom - y0, left - x0 : right - x0] = source_alpha
        return color, alpha

    def _base_region(self, y0, y1, x0, x1):
        transform = self._base_transform.copy()
        transform[:, 2] += transform[:, :2] @ np.array([x0, y0])
        corners = transform @ np.array(
            [
                [0, x1 - x0 - 1, 0, x1 - x0 - 1],
                [0, 0, y1 - y0 - 1, y1 - y0 - 1],
                [1, 1, 1, 1],
            ]
        )
        left, top = np.maximum(0, np.floor(corners.min(axis=1)).astype(int) - 2)
        right, bottom = np.minimum(
            [self.base.shape[1], self.base.shape[0]],
            np.ceil(corners.max(axis=1)).astype(int) + 3,
        )
        if left >= right or top >= bottom:
            return np.zeros((y1 - y0, x1 - x0, 3), np.uint8)
        pixels, _ = self.base.read(top, bottom, left, right)
        transform[:, 2] -= [left, top]
        return cv2.warpAffine(
            pixels,
            transform,
            (x1 - x0, y1 - y0),
            flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
        )

    def _region(self, y0, y1, x0, x1):
        foreground, alpha = self._floor_region(y0, y1, x0, x1)
        background = self._base_region(y0, y1, x0, x1)
        return _blend_regions(foreground, alpha, background, COMPOSITE_DIM)

    def coarse_overview(self, scale: float) -> np.ndarray:
        height, width = self.shape[:2]
        output_height, output_width = round(height * scale), round(width * scale)
        output = np.empty((output_height, output_width, 3), np.uint8)
        area = scale <= 1
        for y0 in range(0, output_height, 128):
            y1 = min(output_height, y0 + 128)
            ys = _axis_taps(y0, y1, scale, height, area)
            for x0 in range(0, output_width, 128):
                x1 = min(output_width, x0 + 128)
                xs = _axis_taps(x0, x1, scale, width, area)
                left, top = xs[0][0][0], ys[0][0][0]
                right = max(indices[-1] for indices, _ in xs) + 1
                bottom = max(indices[-1] for indices, _ in ys) + 1
                source = self._region(top, bottom, left, right)
                horizontal = np.zeros((bottom - top, x1 - x0, 3), np.float32)
                for indices, weights in xs:
                    horizontal += source[:, indices - left] * weights.astype(np.float32)[None, :, None]
                block = np.zeros((y1 - y0, x1 - x0, 3), np.float32)
                for indices, weights in ys:
                    block += horizontal[indices - top] * weights.astype(np.float32)[:, None, None]
                output[y0:y1, x0:x1] = np.rint(block).astype(np.uint8)
        return output

    def coverage(self, scale: float) -> np.ndarray:
        height, width = round(self.shape[0] * scale), round(self.shape[1] * scale)
        output = np.zeros((height, width), bool)
        xs = np.arange(width, dtype=np.float64) / scale
        ys = np.arange(height, dtype=np.float64) / scale
        for row in range(0, height, 128):
            yy = ys[row : row + 128]
            for column in range(0, width, 128):
                xx = xs[column : column + 128]
                x0, y0 = math.floor(xx.min()) - 3, math.floor(yy.min()) - 3
                x1, y1 = math.ceil(xx.max()) + 4, math.ceil(yy.max()) + 4
                _, alpha = self._floor_region(y0, y1, x0, x1)
                support = cv2.dilate((alpha > 0).astype(np.uint8), np.ones((5, 5), np.uint8))
                ix = np.rint(xx - x0).astype(int)
                iy = np.rint(yy - y0).astype(int)
                output[row : row + len(yy), column : column + len(xx)] = support[np.ix_(iy, ix)] > 0
        return output


@dataclass(frozen=True)
class _AtlasItem:
    tier_id: str
    pixels: np.ndarray
    coverage: np.ndarray
    rank_record: dict


def _pack_atlas(items):
    ordered = sorted(items, key=lambda item: item.tier_id)
    positions, x, y, row_height, width = {}, 0, 0, 0, 0
    for item in ordered:
        height, item_width = item.pixels.shape[:2]
        cell_width, cell_height = item_width + 2 * ATLAS_PADDING, height + 2 * ATLAS_PADDING
        if cell_width > MAX_ATLAS_WIDTH:
            raise ValueError(f"楼层图集单元过宽: {item.tier_id}")
        if x and x + cell_width > MAX_ATLAS_WIDTH:
            y += row_height
            x = row_height = 0
        positions[item.tier_id] = (x + ATLAS_PADDING, y + ATLAS_PADDING, item_width, height)
        x += cell_width
        row_height = max(row_height, cell_height)
        width = max(width, x)
    pixels = np.zeros((y + row_height, width, 3), np.uint8)
    coverage = np.zeros((y + row_height, width), np.uint8)
    for item in ordered:
        left, top, item_width, height = positions[item.tier_id]
        pixels[top : top + height, left : left + item_width] = item.pixels
        coverage[top : top + height, left : left + item_width] = item.coverage.astype(np.uint8)
    return pixels, coverage, positions


def build_prepared_localization(root: Path) -> dict:
    root = Path(root).resolve()
    local = root / "localization"
    manifest_path = local / "manifest.json"
    manifest = _json(manifest_path)
    if manifest.get("schema_version") != 3 or manifest.get("kind") != "okef-tiled-localization":
        raise ValueError("预编译资源要求定位瓦片 v3")
    files = {record["path"]: record for record in manifest["files"]}
    catalog = _json(_relative(local, manifest["layer_catalog"]))
    entries = [entry for entry in catalog["layers"] if entry.get("map_category") == "layered"]
    base_entries = {entry["map_id"]: entry for entry in manifest["base_maps"]}
    output = root / PREPARED_DIRNAME
    target_sha = _digest(manifest_path)
    if output.exists():
        raise ValueError(f"预编译资源目录已存在: {output}")
    output.mkdir()

    scale = INPUT_WIDTH / (MATCH_PIXELS_PER_METER * SCALE_CONSTANT) * COARSE_FACTOR
    floors = []
    atlases = {}
    all_records = []
    by_map = {}
    for entry in entries:
        by_map.setdefault(entry["map_name"], []).append(entry)
    for map_id, map_entries in sorted(by_map.items()):
        base_entry = base_entries.get(map_id)
        if base_entry is None:
            raise ValueError(f"楼层缺少同图主图: {map_id}")
        base_index_path = base_entry["image"]
        base_index = _json(_relative(local, base_index_path))
        base = _TiledImage(local, files, base_index_path, layer=False)
        base_matrix = _mapping_matrix(local, base_index)
        items = []
        for entry in sorted(map_entries, key=lambda item: item["id"]):
            foreground = _TiledImage(
                local,
                files,
                f"{manifest['layers_root']}/{entry['path']}",
                layer=True,
            )
            composite = _CompositeFloor(foreground, base, entry, base_matrix)
            pixels = composite.coarse_overview(scale)
            coverage = composite.coverage(scale)
            mask_relative = f"masks/{map_id}/{entry['id']}.bin"
            mask_target = _relative(output, mask_relative)
            mask_target.parent.mkdir(parents=True, exist_ok=True)
            mask_bytes = np.packbits(coverage.reshape(-1), bitorder="big").tobytes()
            mask_target.write_bytes(mask_bytes)
            all_records.append(_record(output, mask_target))
            rank = foreground.index["floor_rank"]
            rank_resource = rank.get("crop", rank)
            rank_relative = f"rank-images/{map_id}/{entry['id']}.webp"
            rank_target = _relative(output, rank_relative)
            rank_target.parent.mkdir(parents=True, exist_ok=True)
            rank_bytes = _read_verified(local, rank_resource)
            rank_target.write_bytes(rank_bytes)
            all_records.append(_record(output, rank_target))
            items.append(_AtlasItem(entry["id"], pixels, coverage, rank))
            floors.append(
                {
                    "tier_id": entry["id"],
                    "map_id": map_id,
                    "level_id": entry.get("level_id"),
                    "map_category": entry.get("map_category"),
                    "bounds": entry["bounds"],
                    "rank_path": rank_relative,
                    "rank_shape": [rank["height"], rank["width"]],
                    "rank_roi": rank["roi"],
                    "mask_path": mask_relative,
                    "mask_shape": list(coverage.shape),
                    "mask_bytes": len(mask_bytes),
                    "mask_sha256": _digest(mask_target),
                    "mask_bit_order": "big",
                    "mask_row_major": True,
                    "coverage_pixels": int(np.count_nonzero(coverage)),
                }
            )
        atlas_pixels, atlas_coverage, positions = _pack_atlas(items)
        atlas_relative = f"atlases/{map_id}-1280.webp"
        atlas_target = _relative(output, atlas_relative)
        atlas_metrics = _save_q90(atlas_pixels, atlas_target)
        all_records.append(_record(output, atlas_target))
        mask_relative = f"atlases/{map_id}-1280-mask.bin"
        mask_target = _relative(output, mask_relative)
        mask_bytes = np.packbits(atlas_coverage.reshape(-1), bitorder="big").tobytes()
        mask_target.write_bytes(mask_bytes)
        all_records.append(_record(output, mask_target))
        atlases[map_id] = {
            "path": atlas_relative,
            "mask_path": mask_relative,
            "shape": list(atlas_pixels.shape[:2]),
            "padding": ATLAS_PADDING,
            "mask_bytes": len(mask_bytes),
            "mask_bit_order": "big",
            "mask_row_major": True,
            **atlas_metrics,
            "regions": {
                tier_id: {"x": left, "y": top, "width": width, "height": height}
                for tier_id, (left, top, width, height) in sorted(positions.items())
            },
        }

    for floor in floors:
        floor["atlas_region"] = atlases[floor["map_id"]]["regions"][floor["tier_id"]]
    floor_index = {
        "schema_version": PREPARED_VERSION,
        "input_width": INPUT_WIDTH,
        "maps": {map_id: [floor for floor in floors if floor["map_id"] == map_id] for map_id in sorted(atlases)},
    }
    floor_index_path = output / "floor-index.json"
    _write_json(floor_index_path, floor_index)
    all_records.append(_record(output, floor_index_path))
    manifest_value = {
        "kind": PREPARED_KIND,
        "schema_version": PREPARED_VERSION,
        "complete": True,
        "source": {
            "release_id": _json(root / "dataset.json")["release_id"],
            "localization_manifest_sha256": target_sha,
            "input_width": INPUT_WIDTH,
            "coarse_factor": COARSE_FACTOR,
            "composite_dim": COMPOSITE_DIM,
            "composite_padding": COMPOSITE_PADDING,
            "webp_quality": 90,
        },
        "coverage": {
            "layered_floors": len(floors),
            "maps": {map_id: len(items) for map_id, items in sorted(by_map.items())},
            "missing_logic_ids": catalog.get("missing_logic_ids", []),
            "mask_encoding": "row-major-msb-first-packbits-zero-tail",
        },
        "atlases": atlases,
        "floors": floors,
        "files": sorted(all_records, key=lambda row: row["path"]),
    }
    prepared_manifest = output / "manifest.json"
    _write_json(prepared_manifest, manifest_value)
    digest = _digest(prepared_manifest)
    validation = verify_prepared_localization(output, allow_missing_metadata=True)
    _write_json(output / "validation.json", validation)
    _write_ready(output / "READY", digest)
    verify_prepared_localization(output)
    return validation


def verify_prepared_localization(root: Path, *, allow_missing_metadata: bool = False) -> dict:
    root = Path(root).resolve()
    manifest_path = root / "manifest.json"
    manifest = _json(manifest_path)
    if (
        manifest.get("kind") != PREPARED_KIND
        or manifest.get("schema_version") != PREPARED_VERSION
        or manifest.get("complete") is not True
    ):
        raise ValueError("预编译定位清单身份或版本不符")
    files = {item["path"]: item for item in manifest["files"]}
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    }
    expected = files.keys() | {"manifest.json"}
    if not allow_missing_metadata:
        expected.update({"validation.json", "READY"})
    if actual != expected:
        raise ValueError(f"预编译目录存在缺项或孤立文件: {sorted(expected ^ actual)[:8]}")
    for name, record in files.items():
        path = _relative(root, name)
        if path.stat().st_size != record["size"] or _digest(path) != record["sha256"]:
            raise ValueError(f"预编译资源摘要不符: {name}")
    if not allow_missing_metadata:
        ready = (root / "READY").read_text(encoding="ascii").strip().split()
        if ready != [PREPARED_KIND, str(PREPARED_VERSION), _digest(manifest_path)]:
            raise ValueError("预编译完成标记不符")
    floors = manifest["floors"]
    if len(floors) != manifest["coverage"]["layered_floors"]:
        raise ValueError("预编译楼层数不符")
    if len({floor["tier_id"] for floor in floors}) != len(floors):
        raise ValueError("预编译楼层身份重复")
    floor_index = _json(root / "floor-index.json")
    if (
        floor_index.get("schema_version") != PREPARED_VERSION
        or floor_index.get("input_width") != INPUT_WIDTH
    ):
        raise ValueError("floor-index.json 身份或输入宽度不符")
    expected_floor_index = {
        map_id: [floor for floor in floors if floor["map_id"] == map_id]
        for map_id in sorted(manifest["atlases"])
    }
    if floor_index.get("maps") != expected_floor_index:
        raise ValueError("floor-index.json 与预编译 manifest 不一致")
    for floor in floors:
        rank = _read_image(_relative(root, floor["rank_path"]), cv2.IMREAD_COLOR)
        expected_rank_shape = tuple(floor["rank_roi"])
        left, top, right, bottom = expected_rank_shape
        if rank.shape != (bottom - top, right - left, 3):
            raise ValueError(f"楼层排序图尺寸不符: {floor['tier_id']}")
        mask = _relative(root, floor["mask_path"]).read_bytes()
        if (
            floor["mask_sha256"] != _sha256_bytes(mask)
            or len(mask) != floor["mask_bytes"]
            or len(mask) != math.ceil(math.prod(floor["mask_shape"]) / 8)
            or floor["mask_bit_order"] != "big"
            or floor["mask_row_major"] is not True
        ):
            raise ValueError(f"楼层覆盖掩码不符: {floor['tier_id']}")
        region = floor["atlas_region"]
        atlas = manifest["atlases"][floor["map_id"]]
        if (
            region["x"] < 0
            or region["y"] < 0
            or region["x"] + region["width"] > atlas["shape"][1]
            or region["y"] + region["height"] > atlas["shape"][0]
        ):
            raise ValueError(f"楼层图集区域越界: {floor['tier_id']}")
    for map_id, atlas in manifest["atlases"].items():
        image = _read_image(_relative(root, atlas["path"]), cv2.IMREAD_COLOR)
        if image.shape != (*tuple(atlas["shape"]), 3):
            raise ValueError(f"竞争图集尺寸不符: {map_id}")
        if _sha256_bytes(image.tobytes()) != atlas["decoded_sha256"]:
            raise ValueError(f"竞争图集解码像素摘要不符: {map_id}")
        mask = _relative(root, atlas["mask_path"]).read_bytes()
        if len(mask) != atlas["mask_bytes"] or len(mask) != math.ceil(math.prod(atlas["shape"]) / 8):
            raise ValueError(f"竞争图集覆盖索引尺寸不符: {map_id}")
    return {
        "layered_floors": len(floors),
        "maps": manifest["coverage"]["maps"],
        "files": len(files),
        "bytes": sum(item["size"] for item in files.values()),
        "webp_quality": manifest["source"]["webp_quality"],
    }
