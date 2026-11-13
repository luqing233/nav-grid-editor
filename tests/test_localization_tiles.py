import tempfile
import time
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from PIL import Image

from nav_grid_editor.services.maps.localization_tiles import (
    _Bundle, _search_payload_size, _tile_payload_size, build_localization_tiles, verify_localization_tiles,
)
from nav_grid_editor.services.maps.localization_native_compaction import _compact_tile, compact_localization_native_tiles
from nav_grid_editor.services.maps.localization_search_compaction import _compact_image, compact_localization_tiles
from nav_grid_editor.services.maps import MapService
from nav_grid_editor.services.maps.dataset_export import _build, _json, _plan, _record, _write_json
from nav_grid_editor.services.maps.dataset_tiled_export import verify_tiled_release


class LocalizationTilesTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        source = self.root / "source"
        grids = self.root / "grids"
        self.originals = source / "layers"
        self.presets = source / "presets"
        self.snapshot = self.root / "snapshot"
        self.output = self.root / "tiled"
        base = source / "maps/m/4/m_4.png"
        base.parent.mkdir(parents=True)
        image = Image.new("RGB", (512, 256), (210, 40, 10))
        image.paste((20, 150, 90), (256, 0, 512, 256))
        image.save(base)
        matrix = [[0.25, 0, -192], [0, -0.25, -448]]
        inverse = [[4, 0, 768], [0, -4, -1792]]
        _write_json(base.with_name("m_4_mapping.json"), {"image_size": {"width": 512, "height": 256},
                                                       "matrix": matrix, "inverse_matrix": inverse})
        preset_records = []
        for column in range(2):
            path = self.presets / f"m/4/{column-3}_7.webp"
            path.parent.mkdir(parents=True, exist_ok=True)
            image.crop((column*256, 0, (column+1)*256, 256)).save(path, "WEBP", quality=90)
            preset_records.append(_record(self.presets, path))
        _write_json(self.presets / "manifest.json", {"schema_version": 1, "kind": "nav-grid-editor-tile-presets",
            "tile_size": 256, "encoding": {"format": "webp", "quality": 90, "method": 4},
            "maps": [{"map": "m", "zoom": "4", "minX": -3, "maxX": -2, "minY": 7, "maxY": 7, "count": 2}],
            "files": preset_records})
        floor = np.full((257, 513, 4), (20, 160, 30, 255), np.uint8)
        floor[::3, :, 3] = 0
        floor[:, -1, 3] = 128
        self.alpha = floor[:, :, 3]
        self.originals.mkdir()
        (self.originals / "floor.png").write_bytes(cv2.imencode(".png", floor)[1].tobytes())
        _write_json(self.originals / "catalog.json", {"schema_version": 1, "missing_logic_ids": ["unavailable"],
            "layers": [{"id": "floor", "map_name": "m", "level_id": "lv", "map_category": "layered",
                        "path": "floor.png", "width": 513, "height": 257,
                        "bounds": {"west": 5, "east": 133, "south": 12, "north": 76}}]})
        self.svc = MapService(data_root=self.root, tiles_root=source, grid2d_dir=grids)
        self.assertTrue(self.svc.save_grid2d("m", "4", {"origin": [0, 0, 0], "cell_size": 1,
                                                       "cells": [[0, 0]], "blocked": [[1, 1]]})["ok"])
        _build(_plan(source, grids), self.snapshot, lambda *args: None)

    def build(self):
        build_localization_tiles(self.presets, self.snapshot, self.originals, self.output)

    def test_standalone_package_copies_q90_and_preserves_partial_tile_alpha(self):
        local = self.snapshot / "localization"
        for name in ("maps/m/4/m_4.webp", "layers/floor.webp"):
            (local / name).unlink()
        self.build()
        verify_localization_tiles(self.output)
        local = self.output / "localization"
        index = _json(local / "maps/m/4/tiles.json")
        self.assertEqual(index["source"]["tile_origin"], [-3, 7])
        self.assertEqual(index["match_raster"]["source_scale"], [1, 1])
        decoded = []
        for i, tile in enumerate(index["tiles"]):
            raw = (local / tile["path"]).read_bytes()
            self.assertEqual(raw, (self.presets / f"m/4/{i-3}_7.webp").read_bytes())
            decoded.append(cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR))
        for overview in index["coarse"]:
            scale = overview["scale"]
            expected = cv2.resize(np.hstack(decoded), None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            with Image.open(local / overview["path"]) as image:
                np.testing.assert_array_equal(np.asarray(image)[:, :, ::-1], expected)
        floor = _json(local / "layers/floor/tiles.json")
        self.assertEqual([(t["width"], t["height"]) for t in floor["tiles"]], [(512, 257), (1, 257)])
        alpha = []
        for tile in floor["tiles"]:
            with Image.open(local / tile["path"]) as image:
                alpha.append(np.asarray(image.convert("RGBA"))[:, :, 3])
        np.testing.assert_array_equal(np.hstack(alpha), self.alpha)
        self.assertEqual((self.output / "m_4.grid.npz").read_bytes(), (self.snapshot / "m_4.grid.npz").read_bytes())

    def test_missing_tile_is_never_published_as_blank(self):
        (self.presets / "m/4/-2_7.webp").unlink()
        with self.assertRaises(FileNotFoundError):
            self.build()
        self.assertFalse(self.output.exists())

    def test_editor_v3_export_uses_live_sources_without_whole_base_image(self):
        (self.root / "source/maps/m/4/m_4.png").unlink()
        (self.originals / "floor.png").unlink()
        raw = (self.snapshot / "localization/layers/floor.webp").read_bytes()
        (self.originals / "floor.webp").write_bytes(raw)
        path = self.originals / "catalog.json"
        catalog = _json(path)
        catalog["layers"][0]["path"] = "floor.webp"
        _write_json(path, catalog)
        levels_path = self.svc.grid2d_dir / "levels.json"
        levels = _json(levels_path)
        levels["generated_at"] = "2026-10-12T11:50:31+00:00"
        _write_json(levels_path, levels)
        saved_levels = levels_path.read_bytes()

        job = self.svc.dataset_export
        self.assertTrue(job.start(str(self.output), export_format="tiled-v3")["ok"])
        deadline = time.monotonic() + 20
        while job.status()["running"] and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(job.status()["running"], job.status())
        self.assertIsNone(job.status()["error"], job.status())
        verify_tiled_release(self.output)
        local = self.output / "localization"
        floor = _json(local / "layers/floor/tiles.json")
        self.assertEqual(floor["source"]["kind"], "existing-floor-webp-lossless-tiles")
        actual = np.zeros((floor["height"], floor["width"], 3), np.uint8)
        for tile in floor["tiles"]:
            if tile["storage"] == "zero":
                continue
            payload = np.frombuffer((local / tile["path"]).read_bytes(), np.uint8)
            image = cv2.imdecode(payload, cv2.IMREAD_UNCHANGED)
            if image.shape[2] == 4:
                image[image[:, :, 3] == 0, :3] = 0
                image = image[:, :, :3]
            x, y = tile["column"] * floor["tile_size"], tile["row"] * floor["tile_size"]
            if tile["storage"] == "roi":
                x += tile["roi"][0]
                y += tile["roi"][1]
            actual[y:y+image.shape[0], x:x+image.shape[1]] = image
        expected = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_UNCHANGED)
        expected[expected[:, :, 3] == 0, :3] = 0
        np.testing.assert_array_equal(actual, expected[:, :, :3])
        self.assertFalse((local / "maps/m/4/m_4.webp").exists())
        report = _json(self.output / "validation.json")
        self.assertEqual((self.output / "levels.json").read_bytes(), saved_levels)
        self.assertEqual(report["missing_layers"][0]["id"], "unavailable")
        self.assertEqual(report["verification"]["tiles_pixel_equal"], 4)
        dataset = _json(self.output / "dataset.json")
        manifest = _json(local / "manifest.json")
        managed_bytes = (self.output / "dataset.json").stat().st_size + dataset["localization_manifest"]["size"]
        managed_bytes += sum(row["size"] for row in (*dataset["navigation_files"], *manifest["files"]))
        self.assertEqual(report["package_bytes_after"], managed_bytes)
        (local / "orphan.webp").write_bytes(raw)
        with self.assertRaisesRegex(ValueError, "孤立"):
            verify_tiled_release(self.output)

    def test_v2_keeps_tiles_navigation_and_calibration_bytes(self):
        self.build()
        compact = self.root / "compact"
        report = compact_localization_tiles(self.output, compact)
        self.assertEqual(report["summary"]["coarse"]["count"], 12)
        self.assertEqual(report["summary"]["floor_rank"]["count"], 1)
        self.assertTrue(all(row["pixel_equal"] for row in report["images"]))
        verify_localization_tiles(compact)
        manifest = _json(compact / "localization/manifest.json")
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(manifest["search"]["image_layout"], "zero-padded-roi")
        for path in (*self.output.glob("*.grid.npz"), self.output / "links.json", self.output / "levels.json",
                     *self.output.rglob("*_mapping.json"), *self.output.glob("localization/objects/*/*.webp")):
            self.assertEqual((compact / path.relative_to(self.output)).read_bytes(), path.read_bytes())

    def test_v3_keeps_search_pixels_and_logical_tile_geometry(self):
        self.build()
        v2, v3 = self.root / "v2", self.root / "v3"
        compact_localization_tiles(self.output, v2)
        compact_localization_native_tiles(v2, v3)
        verify_localization_tiles(v3)
        for path in (v2 / "localization").rglob("tiles.json"):
            a, b = _json(path), _json(v3 / path.relative_to(v2))
            for name in ("coarse", "floor_rank", "match_raster", "width", "height", "tile_size"):
                self.assertEqual(a[name], b[name])
            for a_tile, b_tile in zip(a["tiles"], b["tiles"], strict=True):
                self.assertEqual({k: a_tile[k] for k in ("row", "column", "width", "height")},
                                 {k: b_tile[k] for k in ("row", "column", "width", "height")})

        local = v3 / "localization"
        path = local / "maps/m/4/tiles.json"
        index = _json(path)
        index["tiles"].pop()
        _write_json(path, index)
        manifest_path = local / "manifest.json"
        manifest = _json(manifest_path)
        for record in manifest["files"]:
            if record["path"] == "maps/m/4/tiles.json":
                record.update(_record(local, path))
        _write_json(manifest_path, manifest)
        dataset = _json(v3 / "dataset.json")
        dataset["localization_manifest"] = _record(v3, manifest_path)
        _write_json(v3 / "dataset.json", dataset)
        with self.assertRaisesRegex(ValueError, "瓦片覆盖有缺口"):
            verify_localization_tiles(v3)


class CompactSearchImageTest(unittest.TestCase):
    def test_zero_border_crop_preserves_dark_pixels_islands_and_edges(self):
        cases = (
            ("black", [], [0, 0, 0, 0]),
            ("dark", [(3, 2, (1, 0, 0))], [3, 2, 4, 3]),
            ("islands", [(2, 1, (0, 0, 1)), (4, 3, (0, 2, 0))], [2, 1, 5, 4]),
            ("edge", [(0, 0, (1, 0, 0)), (0, 4, (2, 1, 0))], [0, 0, 1, 5]),
            ("full_roi", [(0, 0, (1, 0, 0)), (6, 4, (2, 1, 0))], [0, 0, 7, 5]),
        )
        with tempfile.TemporaryDirectory() as temp:
            source = _Bundle(Path(temp) / "v1")
            target = _Bundle(Path(temp) / "v2")
            for label, pixels, expected in cases:
                with self.subTest(label=label):
                    image = np.zeros((5, 7, 3), np.uint8)
                    for x, y, color in pixels:
                        image[y, x] = color
                    name = f"{label}.webp"
                    original = source.index_image(name, image)
                    result = _compact_image(source.root, target, original)
                    self.assertEqual((result["width"], result["height"], result["roi"]), (7, 5, expected))
                    self.assertEqual(result["fill"], [0, 0, 0])
                    restored = np.zeros_like(image)
                    if pixels:
                        left, top, right, bottom = expected
                        with Image.open(target.root / name) as payload:
                            self.assertEqual(payload.size, _search_payload_size(result, 2))
                            restored[top:bottom, left:right] = np.asarray(payload)[:, :, ::-1]
                    else:
                        self.assertIsNone(_search_payload_size(result, 2))
                        self.assertFalse((target.root / name).exists())
                    np.testing.assert_array_equal(restored, image)
                    if label == "full_roi":
                        self.assertEqual((target.root / name).read_bytes(), (source.root / name).read_bytes())

    def test_roi_rejects_invalid_geometry_or_missing_nonzero_payload(self):
        base = {"width": 7, "height": 5, "roi": [1, 2, 3, 4], "fill": [0, 0, 0],
                "path": "a.webp", "size": 1, "sha256": "hash"}
        for change in ({"roi": [-1, 0, 3, 4]}, {"roi": [1, 2, 8, 4]}, {"fill": [1, 0, 0]},
                       {"roi": [0, 0, 0, 0]}, {"path": None}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                _search_payload_size({**base, **change}, 2)


class CompactNativeTileTest(unittest.TestCase):
    def test_alpha_mask_preserves_a_dark_touching_pixel_and_can_elide_zero(self):
        with tempfile.TemporaryDirectory() as temp:
            source = _Bundle(Path(temp) / "v2")
            target = _Bundle(Path(temp) / "v3")
            rgba = np.full((11, 17, 4), (110, 20, 80, 0), dtype=np.uint8)
            for alpha in (0, 1):
                rgba[10, 0] = (1, 0, 0, alpha)
                with BytesIO() as buffer:
                    Image.fromarray(rgba).save(buffer, "WEBP", lossless=True, exact=True)
                    raw = buffer.getvalue()
                tile = {**source.write("tile.webp", raw), "width": 17, "height": 11}
                compact = {**tile, **_compact_tile(source.root, target, tile, True)}
                if alpha == 0:
                    self.assertEqual(compact["storage"], "zero")
                    self.assertIsNone(_tile_payload_size(compact, 3))
                else:
                    self.assertEqual(compact["storage"], "full")
                    self.assertEqual(compact["roi"], [0, 10, 1, 11])
                    self.assertEqual(_tile_payload_size(compact, 3), (17, 11))
                    with Image.open(target.root / compact["path"]) as image:
                        self.assertEqual(image.getpixel((0, 10)), (1, 0, 0, 1))

    def test_opaque_black_outside_rgb_roi_and_fully_black_floor_keep_alpha(self):
        with tempfile.TemporaryDirectory() as temp:
            source = _Bundle(Path(temp) / "v2")
            target = _Bundle(Path(temp) / "v3")
            for colored in (False, True):
                pixels = np.zeros((64, 64, 4), np.uint8)
                pixels[:, :, 3] = 255
                if colored:
                    pixels[20:40, 20:40, :3] = (20, 60, 90)
                ok, encoded = cv2.imencode(".webp", pixels, [cv2.IMWRITE_WEBP_QUALITY, 101])
                self.assertTrue(ok)
                tile = {**source.write(f"floor-{colored}.webp", encoded.tobytes()), "width": 64, "height": 64}
                result = {**tile, **_compact_tile(source.root, target, tile, True)}
                self.assertEqual(result["storage"], "full")
                self.assertEqual(result["roi"], [20, 20, 40, 40] if colored else [0, 0, 0, 0])
                self.assertEqual(_tile_payload_size(result, 3), (64, 64))
                self.assertEqual((target.root / result["path"]).read_bytes(), (source.root / tile["path"]).read_bytes())

    def test_larger_lossless_roi_keeps_original_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            source = _Bundle(Path(temp) / "v2")
            target = _Bundle(Path(temp) / "v3")
            bgr = np.zeros((9, 13, 3), np.uint8)
            bgr[2:5, 3:6] = (20, 100, 30)
            tile = source.index_image("tile.webp", bgr)
            with patch("nav_grid_editor.services.maps.localization_native_compaction._lossless_webp",
                       return_value=b"larger" * tile["size"]):
                compact = {**tile, **_compact_tile(source.root, target, tile, False)}
            self.assertEqual(compact["storage"], "full")
            self.assertEqual(compact["roi"], [3, 2, 6, 5])
            self.assertEqual(_tile_payload_size(compact, 3), (13, 9))
            self.assertEqual((target.root / "tile.webp").read_bytes(), (source.root / "tile.webp").read_bytes())

    def test_zero_declaration_requires_zero_roi_and_no_payload(self):
        record = {"storage": "zero", "width": 13, "height": 9, "roi": [0, 0, 0, 0],
                  "path": None, "size": 0, "sha256": None}
        for change in ({"path": "missing.webp"}, {"storage": "full"}, {"roi": [0, 0, 1, 1]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                _tile_payload_size({**record, **change}, 3)
