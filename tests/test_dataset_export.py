"""Snapshot integrity, color/alpha retention and non-destructive publication."""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

from nav_grid_editor.services.maps import MapService
from nav_grid_editor.services.maps import dataset_export as export
from nav_grid_editor.services.maps.grid import _MAX_FLOOR_REGIONS, _MAX_FLOOR_REGION_CELLS


class DatasetExportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tiles, self.grids = self.root / "tiles", self.root / "grids"
        self.base = self.tiles / "maps/m/4/m_4.png"
        self.base.parent.mkdir(parents=True)
        self.color = np.arange(60, dtype=np.uint8).reshape(4, 5, 3) * 4
        self.base.write_bytes(cv2.imencode(".png", self.color)[1].tobytes())
        mapping = {"image_size": {"width": 5, "height": 4},
                   "matrix": [[1, 0, 0], [0, -1, 4]],
                   "inverse_matrix": [[1, 0, 0], [0, -1, 4]]}
        self.mapping = self.base.with_name("m_4_mapping.json")
        self.mapping.write_text(json.dumps(mapping), encoding="utf-8")
        self.layer = self.tiles / "layers/m/lv/layer.png"
        self.layer.parent.mkdir(parents=True)
        self.bgra = np.dstack((self.color, np.full((4, 5), 255, np.uint8)))
        self.bgra[0, 0, 3] = 0
        self.bgra[0, 1, 3] = 128
        self.layer.write_bytes(cv2.imencode(".png", self.bgra)[1].tobytes())
        self.independent = self.layer.with_name("indie.png")
        self.independent.write_bytes(cv2.imencode(".png", np.full((4, 5), 70, np.uint8))[1].tobytes())
        self.catalog = self.tiles / "layers/catalog.json"
        self.catalog.write_text(json.dumps({"schema_version": 1, "missing_logic_ids": ["missing_layer"],
            "layers": [{"id": tier, "map_name": "m", "level_id": level,
                        "map_category": category, "name": tier,
                        "path": f"m/lv/{tier}.png", "width": 5, "height": 4,
                        "bounds": {"west": 0, "east": 5, "south": 0, "north": 4}}
                       for tier, level, category in (("layer", "lv", "layered"),
                                                     ("indie", "indie_scene", "independent"))]}), encoding="utf-8")
        self.svc = MapService(data_root=self.root, tiles_root=self.tiles, grid2d_dir=self.grids)
        for tier in ("", "layer"):
            result = self.svc.save_grid2d("m", "4", {
                "origin": [0, 0, 0], "shape": [3, 3], "cell_size": 1,
                "cells": [[0, 0]], "blocked": [[1, 1]],
            }, tier=tier)
            self.assertTrue(result["ok"], result)
        self.grids.joinpath("links.json").write_text(json.dumps({"schema_version": 2, "links": [{
            "id": "door", "from": {"map": "m", "tier": "layer", "zoom": "4",
                                     "rect": {"x0": 0, "x1": 1, "z0": 0, "z1": 1}},
            "to": {"map": "m", "tier": "", "zoom": "4",
                   "rect": {"x0": 0, "x1": 1, "z0": 0, "z1": 1}},
            "kind": "door", "bidirectional": True, "cost": 1,
        }]}), encoding="utf-8")
        self.job = self.svc.dataset_export

    def wait(self, job=None):
        job = job or self.job
        deadline = time.monotonic() + 15
        while job.status()["running"] and time.monotonic() < deadline:
            time.sleep(0.005)
        result = job.status()
        self.assertFalse(result["running"], result)
        return result

    def run_export(self, target=None):
        target = target or self.root / "output"
        result = self.job.start(str(target))
        self.assertTrue(result["ok"], result)
        return target, self.wait()

    def test_complete_snapshot_preserves_grids_color_and_alpha(self):
        source_bytes = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        out, status = self.run_export()
        self.assertIsNone(status["error"], status)
        export.verify_dataset(out)
        self.assertEqual(status["summary"]["grids"], 2)
        self.assertEqual(status["summary"]["images"], 3)
        data = export._json(out / "dataset.json")
        self.assertEqual(len(data["registrations"]), 3)
        missing = next(x for x in data["registrations"] if x["tier"] == "indie")
        self.assertIsNone(missing["grid"])
        self.assertEqual(missing["coordinate_space_id"], "scene:indie_scene")
        self.assertEqual(len(export._json(out / "levels.json")["layers"]), 2)
        self.assertEqual(export._json(out / "localization/layers/catalog.json")["missing_logic_ids"], ["missing_layer"])
        for key in ("m_4.grid.npz", "m__layer_4.grid.npz"):
            self.assertEqual((out / key).read_bytes(), (self.grids / key).read_bytes())
        images = ("maps/m/4/m_4.webp", "layers/m/lv/layer.webp", "layers/m/lv/indie.webp")
        for name in images:
            path = out / "localization" / name
            self.assertEqual(path.read_bytes()[8:12], b"WEBP")
            decoded = export._read_image(path, cv2.IMREAD_UNCHANGED)
            self.assertEqual(decoded.shape[:2], (4, 5))
        base = export._read_image(out / "localization" / images[0], cv2.IMREAD_COLOR)
        self.assertFalse(np.array_equal(base[:, :, 0], base[:, :, 2]), "彩色主图变成了灰度")
        layer = export._read_image(out / "localization" / images[1], cv2.IMREAD_UNCHANGED)
        self.assertEqual(layer.shape[2], 4)
        np.testing.assert_array_equal(layer[:, :, 3], self.bgra[:, :, 3])
        self.assertFalse(np.array_equal(layer[:, :, 0], layer[:, :, 2]), "彩色楼层变成了灰度")
        manifest = export._json(out / "localization/manifest.json")
        self.assertIn("quality=90 method=4", manifest["provenance"]["processing"])
        catalog = export._json(out / "localization/layers/catalog.json")
        self.assertTrue(all(row["path"].endswith(".webp") for row in catalog["layers"]))
        self.assertIn("*.webp filter=lfs", (out / ".gitattributes").read_text())
        for path, raw in source_bytes.items():
            self.assertEqual(path.read_bytes(), raw, str(path))
        self.assertFalse((out / ".inputs").exists())
        self.assertFalse((out / ".git").exists())

    def test_rejects_nonempty_and_source_paths_without_touching_them(self):
        occupied = self.root / "occupied"
        occupied.mkdir()
        sentinel = occupied / "old.grid.npz"
        sentinel.write_bytes(b"keep")
        for target in (occupied, self.root, self.tiles, self.grids / "nested", self.root / "configs/out"):
            with self.subTest(target=target):
                self.assertFalse(self.job.start(str(target))["ok"])
        self.assertEqual(sentinel.read_bytes(), b"keep")
        self.assertFalse(list(self.root.glob(".*.export-*")))

    def test_webp_source_preferred_over_legacy_png_and_manifest_paths_match(self):
        from PIL import Image
        source = self.base.with_suffix(".webp")
        # Distinct content proves the exporter selects the new file over stale PNG.
        Image.new("RGB", (5, 4), (80, 80, 80)).save(source, "WEBP", lossless=True)
        mapping_path = self.base.with_name("m_4_mapping.json")
        mapping = export._json(mapping_path)
        mapping["map_name"] = "m_4.png"
        export._write_json(mapping_path, mapping)
        out, status = self.run_export()
        self.assertIsNone(status["error"], status)
        manifest = export._json(out / "localization/manifest.json")
        base = manifest["base_maps"][0]
        self.assertEqual(base["image"], "maps/m/4/m_4.webp")
        pixels = export._read_image(out / "localization" / base["image"], cv2.IMREAD_GRAYSCALE)
        np.testing.assert_array_equal(pixels, np.full((4, 5), 80, np.uint8))
        self.assertEqual(export._json(out / "localization" / base["mapping"])["map_name"], "m_4.webp")
        self.assertEqual(export._json(mapping_path)["map_name"], "m_4.png")
        self.assertEqual(manifest["schema_version"], 1)
        export.verify_dataset(out)

    def test_empty_target_is_published_only_after_validation(self):
        target = self.root / "empty"
        target.mkdir()
        with mock.patch.object(export, "_color_webp", side_effect=ValueError("decode failed")):
            _, status = self.run_export(target)
        self.assertIn("decode failed", status["error"])
        self.assertTrue(target.is_dir())
        self.assertEqual(list(target.iterdir()), [])
        self.assertFalse(list(self.root.glob(".*.export-*")))
        _, status = self.run_export(target)
        self.assertIsNone(status["error"])

    def test_bad_mapping_or_missing_declared_image_does_not_publish(self):
        self.mapping.write_text("{}", encoding="utf-8")
        out, status = self.run_export()
        self.assertIsNotNone(status["error"])
        self.assertFalse(out.exists())
        self.independent.unlink()
        out, status = self.run_export()
        self.assertIsNotNone(status["error"])
        self.assertFalse(out.exists())

    def test_changed_source_or_new_grid_aborts_frozen_snapshot(self):
        original = export._color_webp
        for add_file in (False, True):
            with self.subTest(add_file=add_file):
                changed = False
                def change(*args, **kwargs):
                    nonlocal changed
                    if not changed:
                        changed = True
                        if add_file:
                            (self.grids / "new_4.grid.npz").write_bytes(b"new")
                        else:
                            self.mapping.write_text(self.mapping.read_text() + " ", encoding="utf-8")
                    return original(*args, **kwargs)
                with mock.patch.object(export, "_color_webp", side_effect=change):
                    out, status = self.run_export()
                self.assertIsNotNone(status["error"])
                self.assertFalse(out.exists())

    def test_duplicate_jobs_and_cross_instance_target_lock(self):
        entered, release = threading.Event(), threading.Event()
        original = export._color_webp
        def paused(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(10))
            return original(*args, **kwargs)
        target = self.root / "output"
        with mock.patch.object(export, "_color_webp", side_effect=paused):
            try:
                self.assertTrue(self.job.start(str(target))["ok"])
                self.assertTrue(entered.wait(5))
                self.assertFalse(self.job.start(str(target))["ok"])
                other = export.DatasetExport(self.root, self.tiles, self.grids)
                self.assertTrue(other.start(str(target))["ok"])
                self.assertIsNotNone(self.wait(other)["error"])
            finally:
                release.set()
            self.assertIsNone(self.wait()["error"])

    def test_catalog_escape_or_grid_identity_mismatch_fails_closed(self):
        data = export._json(self.catalog)
        data["layers"][0]["path"] = "../../credentials.json"
        self.catalog.write_text(json.dumps(data), encoding="utf-8")
        out, status = self.run_export()
        self.assertIsNotNone(status["error"])
        self.assertFalse(out.exists())
        data["layers"][0]["path"] = "m/lv/layer.png"
        self.catalog.write_text(json.dumps(data), encoding="utf-8")
        (self.grids / "m_4.grid.npz").rename(self.grids / "wrong_4.grid.npz")
        out, status = self.run_export()
        self.assertIn("身份", status["error"])
        self.assertFalse(out.exists())

    def test_destination_populated_during_export_is_not_deleted(self):
        target = self.root / "output"
        original = export._color_webp
        def occupy(*args, **kwargs):
            target.mkdir(exist_ok=True)
            (target / "keep.txt").write_text("keep", encoding="utf-8")
            return original(*args, **kwargs)
        with mock.patch.object(export, "_color_webp", side_effect=occupy):
            _, status = self.run_export(target)
        self.assertIsNotNone(status["error"])
        self.assertEqual((target / "keep.txt").read_text(), "keep")
        self.assertEqual(len(list(target.iterdir())), 1)

    def test_hash_corruption_is_rejected(self):
        out, status = self.run_export()
        self.assertIsNone(status["error"])
        (out / "m_4.grid.npz").write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "清单校验失败"):
            export.verify_dataset(out)

    def test_default_target_is_unique_and_does_not_mutate_source(self):
        result = self.job.start()
        self.assertTrue(result["ok"])
        self.assertTrue(Path(result["output"]).is_relative_to(self.root / "exports"))
        self.assertIsNone(self.wait()["error"])
        result2 = self.job.start()
        self.assertNotEqual(result2["output"], result["output"])
        self.assertIsNone(self.wait()["error"])

    def test_index_only_declaration_and_grid_without_visual_are_preserved(self):
        self.assertTrue(self.svc.save_grid2d("m", "4", {
            "origin": [0, 0, 0], "cell_size": 1, "cells": [[0, 0]],
        }, tier="new_layer")["ok"])
        levels = export._json(self.grids / "levels.json")
        levels["layers"].append({"id": "new_layer", "map_name": "m", "level_id": "new_region",
                                 "map_category": "layered", "name": "尚无底图的新区域"})
        levels["layers"].append({"id": "declared_only", "map_name": "m", "level_id": "future_region",
                                 "map_category": "layered"})
        (self.grids / "levels.json").write_text(json.dumps(levels), encoding="utf-8")
        out, status = self.run_export()
        self.assertIsNone(status["error"], status)
        self.assertEqual(len(export._json(out / "levels.json")["layers"]), 4)
        data = export._json(out / "dataset.json")
        grid_only = next(r for r in data["registrations"] if r["tier"] == "new_layer")
        self.assertIsNone(grid_only["visual"])
        self.assertEqual(grid_only["grid"], "m__new_layer_4.grid.npz")
        self.assertFalse(any(r["tier"] == "declared_only" for r in data["registrations"]))

    def test_invalid_floor_metadata_aborts_export_without_rewriting_source(self):
        path = self.grids / "m_4.grid.npz"
        with np.load(path, allow_pickle=False) as saved:
            cells = saved["cells"]
            meta = json.loads(saved["meta"].item())
        valid = {"x0": 0, "x1": 1, "z0": 0, "z1": 1, "y_min": 2, "y_max": 3}
        cases = {
            "non_array": {},
            "non_object": [None],
            "missing_fields": [{"x0": 0}],
            "non_numeric": [{**valid, "y_min": "wrong"}],
            "nan": [{**valid, "x0": float("nan")}],
            "infinity": [{**valid, "y_max": float("inf")}],
            "inverted_x": [{**valid, "x0": 2}],
            "inverted_z": [{**valid, "z0": 2}],
            "inverted_y": [{**valid, "y_min": 4}],
            "cells_not_array": [{**valid, "cells": {}}],
            "cell_shape": [{**valid, "cells": [[0, 1, 0]]}],
            "cell_non_numeric": [{**valid, "cells": [[0, "wrong", 0, 1]]}],
            "cell_non_finite": [{**valid, "cells": [[0, 1, 0, float("inf")]]}],
            "cell_inverted_x": [{**valid, "cells": [[2, 1, 0, 1]]}],
            "cell_inverted_z": [{**valid, "cells": [[0, 1, 2, 1]]}],
            "region_limit": [valid] * (_MAX_FLOOR_REGIONS + 1),
            "cell_limit": [{**valid, "cells": [[0, 1, 0, 1]] * (_MAX_FLOOR_REGION_CELLS + 1)}],
        }
        for name, regions in cases.items():
            with self.subTest(case=name):
                meta["floor_regions"] = regions
                np.savez_compressed(path, cells=cells, meta=np.array(json.dumps(meta)))
                original = path.read_bytes()
                out, status = self.run_export()
                self.assertIn("m_4.grid.npz", status["error"])
                self.assertIn("floor_regions", status["error"])
                self.assertFalse(out.exists())
                self.assertEqual(path.read_bytes(), original)
                self.assertFalse(list(self.root.glob(".*.export*")))

    def test_valid_optional_floor_metadata_is_validated_without_normalizing_npz(self):
        path = self.grids / "m_4.grid.npz"
        with np.load(path, allow_pickle=False) as saved:
            cells = saved["cells"]
            meta = json.loads(saved["meta"].item())
        for number, regions in enumerate((None, [], [{
            "x0": "0", "x1": 1, "z0": 0, "z1": 1, "y_min": 2, "y_max": 2,
            "cells": [[0, 1, 0, 1]], "component_id": " preserved ",
        }])):
            with self.subTest(regions=regions):
                meta["floor_regions"] = regions
                np.savez_compressed(path, cells=cells, meta=np.array(json.dumps(meta)))
                original = path.read_bytes()
                out, status = self.run_export(self.root / f"valid-floor-{number}")
                self.assertIsNone(status["error"], status)
                self.assertEqual((out / path.name).read_bytes(), original)
                self.assertEqual(path.read_bytes(), original)
