# -*- coding: utf-8 -*-
"""Grid-metadata based floor resolution tests."""

import json
import tempfile
import unittest
from pathlib import Path

from nav_grid_editor.services.maps import MapService


class FloorResolutionTest(unittest.TestCase):
    def _service(self):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        (root / "t" / "layers").mkdir(parents=True)
        (root / "t" / "layers" / "catalog.json").write_text(
            json.dumps({
                "schema_version": 1,
                "layers": [
                    {
                        "id": "scene_map02_lv005_layer_tips_7",
                        "map_name": "map02",
                        "level_id": "map02_lv005",
                        "name": "测试区",
                    },
                    {
                        "id": "scene_map02_lv005_layer_tips_6",
                        "map_name": "map02",
                        "level_id": "map02_lv005",
                        "name": "测试区深处",
                    },
                ],
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        svc = MapService(
            data_root=root,
            tiles_root=root / "t",
            profile_dir=root / "p",
            grid2d_dir=root / "g",
        )
        return temp, root, svc

    def _save_grid(self, svc, tier, floor_regions):
        result = svc.save_grid2d(
            "map02", "4",
            {
                "origin": [0, 0, 0],
                "shape": [2, 2],
                "cell_size": 1,
                "cells": [[0, 0]],
                "blocked": [],
                "floor_regions": floor_regions,
            },
            tier=tier,
        )
        self.assertTrue(result["ok"], result)

    def test_grid_height_regions_resolve_unique_floor(self):
        temp, _root, svc = self._service()
        try:
            self._save_grid(svc, "scene_map02_lv005_layer_tips_7", [
                {"x0": -10, "x1": 10, "z0": -10, "z1": 10,
                 "y_min": 200, "y_max": 250},
            ])
            self._save_grid(svc, "scene_map02_lv005_layer_tips_6", [
                {"x0": -10, "x1": 10, "z0": -10, "z1": 10,
                 "y_min": 260, "y_max": 300},
            ])

            upper = svc.resolve_tier_by_height("map02", 0, 230, 0)
            lower = svc.resolve_tier_by_height("map02", 0, 280, 0)
            gap = svc.resolve_tier_by_height("map02", 0, 255, 0)

            self.assertEqual(upper["status"], "unique")
            self.assertEqual(upper["tier_id"], "scene_map02_lv005_layer_tips_7")
            self.assertEqual(lower["status"], "unique")
            self.assertEqual(lower["tier_id"], "scene_map02_lv005_layer_tips_6")
            self.assertEqual(gap["status"], "main")
            self.assertEqual(gap["reason"], "no_region_covers")
        finally:
            temp.cleanup()

    def test_overlapping_height_regions_are_ambiguous(self):
        temp, _root, svc = self._service()
        try:
            region = {
                "x0": -10, "x1": 10, "z0": -10, "z1": 10,
                "y_min": 200, "y_max": 260,
            }
            self._save_grid(
                svc, "scene_map02_lv005_layer_tips_7", [region])
            self._save_grid(
                svc, "scene_map02_lv005_layer_tips_6", [region])

            result = svc.resolve_tier_by_height("map02", 0, 230, 0)
            self.assertEqual(result["status"], "ambiguous")
            self.assertEqual(set(result["matched"]), {
                "scene_map02_lv005_layer_tips_7",
                "scene_map02_lv005_layer_tips_6",
            })
        finally:
            temp.cleanup()

    def test_grid_metadata_roundtrips_floor_regions(self):
        temp, _root, svc = self._service()
        try:
            regions = [{
                "x0": -1, "x1": 2, "z0": -3, "z1": 4,
                "y_min": 10, "y_max": 20,
            }]
            self._save_grid(
                svc, "scene_map02_lv005_layer_tips_7", regions)
            loaded = svc.get_grid2d(
                "map02", "4", "scene_map02_lv005_layer_tips_7")
            self.assertEqual(loaded["data"]["floor_regions"], regions)
        finally:
            temp.cleanup()

    def test_metadata_only_update_keeps_grid_cells(self):
        temp, _root, svc = self._service()
        try:
            self._save_grid(
                svc, "scene_map02_lv005_layer_tips_7", [])
            regions = [{
                "x0": -5, "x1": 5, "z0": -6, "z1": 6,
                "y_min": 100, "y_max": 140,
            }]
            saved = svc.save_grid_floor_regions(
                "map02", "4", "scene_map02_lv005_layer_tips_7", regions)
            self.assertTrue(saved["ok"], saved)
            loaded = svc.get_grid2d(
                "map02", "4", "scene_map02_lv005_layer_tips_7")
            self.assertEqual(loaded["data"]["floor_regions"], regions)
            self.assertEqual(loaded["data"]["cells"], [[0, 0]])
        finally:
            temp.cleanup()

    def test_floor_map_lists_all_tiers_for_level(self):
        temp, _root, svc = self._service()
        try:
            self._save_grid(svc, "scene_map02_lv005_layer_tips_7", [{
                "x0": -10, "x1": 10, "z0": -20, "z1": 20,
                "y_min": 200, "y_max": 240,
            }])
            result = svc.get_grid_floor_map("map02", "4", "map02_lv005")
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["layers"]), 2)
            by_id = {item["tier_id"]: item for item in result["layers"]}
            self.assertTrue(by_id["scene_map02_lv005_layer_tips_7"]["has_grid"])
            self.assertEqual(
                by_id["scene_map02_lv005_layer_tips_7"]["floor_regions"][0]["y_min"],
                200,
            )
            self.assertFalse(by_id["scene_map02_lv005_layer_tips_6"]["has_grid"])
        finally:
            temp.cleanup()

    def test_actual_grid_cells_find_xz_and_3d_overlaps(self):
        temp, _root, svc = self._service()
        try:
            common = {
                "origin": [0, 0, 0],
                "shape": [2, 2],
                "cell_size": 1,
            }
            svc.save_grid2d(
                "map02", "4",
                {**common, "cells": [[0, 0], [1, 1]], "blocked": []},
                tier="scene_map02_lv005_layer_tips_7",
            )
            svc.save_grid2d(
                "map02", "4",
                {**common, "cells": [[0, 0], [1, 0]], "blocked": []},
                tier="scene_map02_lv005_layer_tips_6",
            )
            xz = svc.get_grid_overlaps(
                "map02", "4",
                "scene_map02_lv005_layer_tips_7", "map02_lv005")
            self.assertEqual(xz["xz_only"], [[0, 0]])
            self.assertEqual(xz["three_d"], [])

            region = {
                "x0": -1, "x1": 2, "z0": -1, "z1": 2,
                "y_min": 10, "y_max": 20,
            }
            svc.save_grid_floor_regions(
                "map02", "4", "scene_map02_lv005_layer_tips_7", [region])
            svc.save_grid_floor_regions(
                "map02", "4", "scene_map02_lv005_layer_tips_6", [region])
            three_d = svc.get_grid_overlaps(
                "map02", "4",
                "scene_map02_lv005_layer_tips_7", "map02_lv005")
            self.assertEqual(three_d["xz_only"], [])
            self.assertEqual(three_d["three_d"], [[0, 0]])
            self.assertEqual(len(three_d["components"]), 1)
            component = three_d["components"][0]
            self.assertEqual(component["severity"], "three_d")
            self.assertEqual(set(component["layers"]), {
                "scene_map02_lv005_layer_tips_7",
                "scene_map02_lv005_layer_tips_6",
            })
        finally:
            temp.cleanup()

    def test_define_height_for_connected_overlap_component(self):
        temp, _root, svc = self._service()
        try:
            common = {
                "origin": [0, 0, 0],
                "shape": [2, 2],
                "cell_size": 1,
                "cells": [[0, 0]],
                "blocked": [],
            }
            svc.save_grid2d(
                "map02", "4", dict(common),
                tier="scene_map02_lv005_layer_tips_7")
            svc.save_grid2d(
                "map02", "4", dict(common),
                tier="scene_map02_lv005_layer_tips_6")
            overlaps = svc.get_grid_overlaps(
                "map02", "4",
                "scene_map02_lv005_layer_tips_7", "map02_lv005")
            component = overlaps["components"][0]
            svc.save_grid_floor_regions(
                "map02", "4", "scene_map02_lv005_layer_tips_7", [{
                    "component_id": "stale-old-component",
                    "x0": 9, "x1": 10, "z0": 9, "z1": 10,
                    "cells": [[9, 10, 9, 10]],
                    "y_min": 1, "y_max": 2,
                }])
            result = svc.save_overlap_component_height(
                "map02", "4",
                "scene_map02_lv005_layer_tips_7",
                "map02_lv005",
                component["id"],
                [
                    {"tier_id": "scene_map02_lv005_layer_tips_7",
                     "y_min": 100, "y_max": 120},
                    {"tier_id": "scene_map02_lv005_layer_tips_6",
                     "y_min": 200, "y_max": 220},
                ],
                replace_component_ids={
                    "scene_map02_lv005_layer_tips_7": [
                        "stale-old-component",
                    ],
                },
            )
            self.assertTrue(result["ok"], result)
            saved_grid = svc.get_grid2d(
                "map02", "4", "scene_map02_lv005_layer_tips_7")
            self.assertEqual(len(saved_grid["data"]["floor_regions"]), 1)
            self.assertEqual(
                saved_grid["data"]["floor_regions"][0]["component_id"],
                component["id"],
            )
            resolved = svc.resolve_tier_by_height("map02", 0.5, 110, 0.5)
            self.assertEqual(resolved["status"], "unique")
            self.assertEqual(
                resolved["tier_id"], "scene_map02_lv005_layer_tips_7")
        finally:
            temp.cleanup()

    def test_main_grid_participates_in_overlap_and_height(self):
        temp, _root, svc = self._service()
        try:
            grid = {
                "origin": [0, 0, 0],
                "shape": [2, 2],
                "cell_size": 1,
                "cells": [[0, 0]],
                "blocked": [],
            }
            svc.save_grid2d("map02", "4", dict(grid), tier="")
            svc.save_grid2d(
                "map02", "4", dict(grid),
                tier="scene_map02_lv005_layer_tips_7")
            overlaps = svc.get_grid_overlaps(
                "map02", "4",
                "scene_map02_lv005_layer_tips_7", "map02_lv005")
            component = overlaps["components"][0]
            self.assertIn("__main__", component["layers"])
            result = svc.save_overlap_component_height(
                "map02", "4",
                "scene_map02_lv005_layer_tips_7",
                "map02_lv005",
                component["id"],
                [
                    {"tier_id": "__main__", "y_min": 100, "y_max": 120},
                    {"tier_id": "scene_map02_lv005_layer_tips_7",
                     "y_min": 200, "y_max": 220},
                ],
            )
            self.assertTrue(result["ok"], result)
            main = svc.get_grid2d("map02", "4", "")
            self.assertEqual(len(main["data"]["floor_regions"]), 1)
            self.assertEqual(
                main["data"]["floor_regions"][0]["component_id"],
                component["id"],
            )
            from_main = svc.get_grid_overlaps(
                "map02", "4", "__main__", "")
            self.assertTrue(from_main["ok"], from_main)
            self.assertEqual(from_main["components"][0]["layers"][0], "__main__")
            floor_map = svc.get_grid_floor_map("map02", "4", "")
            tier_ids = {item["tier_id"] for item in floor_map["layers"]}
            self.assertIn("__main__", tier_ids)
            self.assertIn("scene_map02_lv005_layer_tips_7", tier_ids)
        finally:
            temp.cleanup()

    def test_clear_main_grid_regions_targets_main_file(self):
        """前端主地图的 tier 是 __main__，清空时不能生成 __main__ 这种伪文件名。"""
        temp, root, svc = self._service()
        try:
            grid = {
                "origin": [0, 0, 0],
                "shape": [2, 2],
                "cell_size": 1,
                "cells": [[0, 0]],
                "blocked": [],
            }
            svc.save_grid2d("map02", "4", dict(grid), tier="")
            svc.save_grid2d(
                "map02", "4", dict(grid),
                tier="scene_map02_lv005_layer_tips_7")
            component = svc.get_grid_overlaps(
                "map02", "4", "__main__", "")["components"][0]
            self.assertTrue(svc.save_overlap_component_height(
                "map02", "4", "__main__", "", component["id"],
                [{"tier_id": "__main__", "y_min": 100, "y_max": 120}],
            )["ok"])
            self.assertEqual(
                len(svc.get_grid2d("map02", "4", "")["data"]["floor_regions"]),
                1,
            )
            cleared = svc.save_grid_floor_regions("map02", "4", "__main__", [])
            self.assertTrue(cleared["ok"], cleared)
            self.assertEqual(
                svc.get_grid2d("map02", "4", "")["data"]["floor_regions"], [])
            stray = list(root.glob("g/**/*__main__*"))
            self.assertEqual(stray, [])
        finally:
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()
