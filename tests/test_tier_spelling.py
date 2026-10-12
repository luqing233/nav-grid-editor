# -*- coding: utf-8 -*-
"""主图 tier 的两种拼写与它们之间的归一化。

主图在 API 口径下是 `__main__`、落盘口径下是空串。两种都必须存在（文件名里不能出现
`__main__`，而 API 里空串表达不出「就是主图」而不是「没选分层」），所以转换只经
`settings.disk_tier` / `settings.api_tier`。

这里钉住的是**内联转换会漂移**这件事：服务端曾经因为少了一次 `disk_tier`，让
`grid2d_path(map, zoom, "__main__")` 拼出 `map02____main___4.grid.npz` 这种伪文件
名（`valid_grid_tier("__main__")` 是 True，所以校验拦不住它）。
"""

import json
import tempfile
import unittest
from pathlib import Path

from nav_grid_editor.services.maps import MapService
from nav_grid_editor.services.maps.settings import (
    MAIN_TIER_ID,
    api_tier,
    disk_tier,
    valid_grid_tier,
)


class TierSpellingTest(unittest.TestCase):
    def test_constants_and_round_trip(self):
        self.assertEqual(MAIN_TIER_ID, "__main__")
        self.assertEqual(disk_tier(MAIN_TIER_ID), "")
        self.assertEqual(disk_tier(""), "")
        self.assertEqual(api_tier(""), MAIN_TIER_ID)
        self.assertEqual(api_tier(MAIN_TIER_ID), MAIN_TIER_ID)
        # 分层两边都不变
        for tier in ("scene_map02_lv005_layer_tips_7", "tier_a"):
            self.assertEqual(disk_tier(tier), tier)
            self.assertEqual(api_tier(tier), tier)
        # 幂等
        for value in ("", MAIN_TIER_ID, "tier_a"):
            self.assertEqual(disk_tier(disk_tier(value)), disk_tier(value))
            self.assertEqual(api_tier(api_tier(value)), api_tier(value))

    def test_validation_alone_does_not_catch_the_main_spelling(self):
        """这条是上面那段 docstring 的证据：光靠校验拦不住 `__main__`。"""
        self.assertTrue(valid_grid_tier(MAIN_TIER_ID))
        self.assertTrue(valid_grid_tier(""))


class TierSpellingServiceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.svc = MapService(
            data_root=self.root,
            tiles_root=self.root / "tiles",
            profile_dir=self.root / "profile",
            grid2d_dir=self.root / "grids",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_grid_path_accepts_both_spellings(self):
        """落盘路径是唯一该拿到空串的地方，所以两种口径都得能进。"""
        main_disk = self.svc.grid2d_path("map02", "4", "")
        main_api = self.svc.grid2d_path("map02", "4", MAIN_TIER_ID)
        self.assertEqual(main_disk.name, "map02_4.grid.npz")
        self.assertEqual(main_api, main_disk)
        self.assertNotIn(MAIN_TIER_ID, main_api.name)
        tiered = self.svc.grid2d_path("map02", "4", "tier_a")
        self.assertEqual(tiered.name, "map02__tier_a_4.grid.npz")

    def test_save_and_read_accept_the_api_spelling(self):
        """客户端送 __main__ 必须落到主图那个文件，而不是造一个伪文件名。"""
        data = {"origin": [0, 0, 0], "cell_size": 1,
                "shape": [4, 4], "cells": [[1, 1]], "blocked": []}
        saved = self.svc.save_grid2d("map02", "4", dict(data),
                                     tier=MAIN_TIER_ID)
        self.assertTrue(saved["ok"], saved)
        self.assertEqual(Path(saved["path"]).name, "map02_4.grid.npz")
        self.assertEqual(
            sorted(p.name for p in (self.root / "grids").glob("*.npz")),
            ["map02_4.grid.npz"])
        # 两种口径读回同一份
        a = self.svc.get_grid2d("map02", "4", "")
        b = self.svc.get_grid2d("map02", "4", MAIN_TIER_ID)
        self.assertEqual(a["data"]["cells"], [[1, 1]])
        self.assertEqual(b["data"]["cells"], [[1, 1]])

    def test_floor_regions_endpoint_accepts_the_api_spelling(self):
        self.svc.save_grid2d("map02", "4", {
            "origin": [0, 0, 0], "cell_size": 1, "shape": [4, 4],
            "cells": [[0, 0]], "blocked": [],
        }, tier="")
        regions = [{"x0": 0, "x1": 1, "z0": 0, "z1": 1,
                    "y_min": 10, "y_max": 20}]
        saved = self.svc.save_grid_floor_regions(
            "map02", "4", MAIN_TIER_ID, regions)
        self.assertTrue(saved["ok"], saved)
        self.assertEqual(Path(saved["path"]).name, "map02_4.grid.npz")
        self.assertEqual(
            self.svc.get_grid2d("map02", "4", "")["data"]["floor_regions"],
            regions)

    def test_resolver_reports_main_in_api_spelling(self):
        """判定结果里的 tier_id 与 nav_graph / floor_map 同口径。"""
        self.svc.save_grid2d("map02", "4", {
            "origin": [0, 0, 0], "cell_size": 1, "shape": [4, 4],
            "cells": [[0, 0]], "blocked": [],
            "floor_regions": [{"x0": 0, "x1": 1, "z0": 0, "z1": 1,
                               "y_min": 100, "y_max": 110}],
        }, tier="")
        got = self.svc.resolve_tier_by_height("map02", 0.5, 105, 0.5)
        self.assertEqual(got["status"], "unique")
        self.assertEqual(got["tier_id"], MAIN_TIER_ID)
        self.assertEqual(got["matched"], [MAIN_TIER_ID])
        # 和另外两个输出面对同一概念用同一个词
        graph = self.svc.nav_graph("map02", "4", summary=True)
        self.assertIn(MAIN_TIER_ID, [t["tier_id"] for t in graph["tiers"]])
        floor_map = self.svc.get_grid_floor_map("map02", "4", "")
        self.assertIn(MAIN_TIER_ID, [t["tier_id"] for t in floor_map["layers"]])

    def test_ambiguous_matched_uses_the_api_spelling_too(self):
        for tier, (lo, hi) in (("", (100, 210)), ("tier_a", (190, 300))):
            self.svc.save_grid2d("map02", "4", {
                "origin": [0, 0, 0], "cell_size": 1, "shape": [4, 4],
                "cells": [[0, 0]], "blocked": [],
                "floor_regions": [{"x0": 0, "x1": 1, "z0": 0, "z1": 1,
                                   "y_min": lo, "y_max": hi}],
            }, tier=tier)
        got = self.svc.resolve_tier_by_height("map02", 0.5, 200, 0.5)
        self.assertEqual(got["status"], "ambiguous")
        self.assertEqual(sorted(got["matched"]), sorted([MAIN_TIER_ID, "tier_a"]))
        self.assertEqual(got["tier_id"], "")


if __name__ == "__main__":
    unittest.main()
