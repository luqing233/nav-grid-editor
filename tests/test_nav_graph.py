# -*- coding: utf-8 -*-
"""分层导航图的组装与就绪判定。

规则：层内相邻、层间只走 links、主地图不挖洞（地表那格是真实可走的，地下由
floor_regions 的 Y 区间区分）。这些用例把这个规则钉住，避免以后有人「顺手」
把两层按 X/Z 叠起来当成隐式连边。
"""

import json
import tempfile
import unittest
from pathlib import Path

from nav_grid_editor.services.maps import MapService

TIER_A = "scene_map02_lv005_layer_tips_7"
TIER_B = "scene_map02_lv005_layer_tips_6"


class NavGraphTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / "t" / "layers").mkdir(parents=True)
        (root / "t" / "layers" / "catalog.json").write_text(
            json.dumps({
                "schema_version": 1,
                "layers": [
                    {"id": TIER_A, "map_name": "map02",
                     "level_id": "map02_lv005", "name": "测试区"},
                    {"id": TIER_B, "map_name": "map02",
                     "level_id": "map02_lv005", "name": "测试区深处"},
                ],
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        self.svc = MapService(
            data_root=root,
            tiles_root=root / "t",
            profile_dir=root / "p",
            grid2d_dir=root / "g",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _grid(self, tier, cells, regions=None):
        """所有层共用同一套 origin/cell_size，于是同一 [ix,iz] 就是同一世界格。"""
        result = self.svc.save_grid2d("map02", "4", {
            "origin": [0, 0, 0], "cell_size": 1, "shape": [4, 4],
            "cells": cells, "blocked": [],
            "floor_regions": regions or [],
        }, tier=tier)
        self.assertTrue(result["ok"], result)

    def _link(self, from_tier, to_tier, kind="stairs"):
        result = self.svc.save_grid_link({
            "from": {"map": "map02", "tier": from_tier, "zoom": "4",
                     "rect": {"x0": 0, "z0": 0, "x1": 1, "z1": 1}},
            "to": {"map": "map02", "tier": to_tier, "zoom": "4",
                   "rect": {"x0": 0, "z0": 0, "x1": 1, "z1": 1}},
            "kind": kind,
        })
        self.assertTrue(result["ok"], result)
        return result["link"]["id"]

    @staticmethod
    def _region(y_min, y_max):
        return [{"x0": 0, "x1": 1, "z0": 0, "z1": 1,
                 "y_min": y_min, "y_max": y_max}]

    def test_tiers_expose_free_cells_and_no_implicit_cross_tier_edges(self):
        self._grid("", [[0, 0]])
        self._grid(TIER_A, [[0, 0], [1, 1]])
        graph = self.svc.nav_graph("map02", "4")
        self.assertTrue(graph["ok"], graph)
        by_id = {tier["tier_id"]: tier for tier in graph["tiers"]}
        self.assertEqual(set(by_id), {"__main__", TIER_A})
        self.assertEqual(by_id[TIER_A]["free_cells"], [[0, 0], [1, 1]])
        self.assertEqual(by_id[TIER_A]["origin"], [0.0, 0.0])
        self.assertEqual(by_id[TIER_A]["cell_size"], 1.0)
        self.assertEqual(by_id[TIER_A]["shape"], [4, 4])
        # 跨层边只可能来自 links：图里不能出现任何隐式跨层邻接
        self.assertEqual(graph["cross_tier_edges_from"], "links")

    def test_map_without_any_grid_is_not_ready(self):
        graph = self.svc.nav_graph("map02", "4")
        self.assertFalse(graph["ok"])
        self.assertIn("没有任何网格文件", graph["error"])

    def test_dangling_link_blocks_readiness(self):
        self._grid("", [[0, 0]])
        self._grid(TIER_A, [[2, 2]])
        upsert = self.svc.save_grid_link({
            "from": {"map": "map02", "tier": "", "zoom": "4",
                     "rect": {"x0": 0, "z0": 0, "x1": 1, "z1": 1}},
            "to": {"map": "map02", "tier": TIER_A, "zoom": "4",
                   "rect": {"x0": 0, "z0": 0, "x1": 1, "z1": 1}},
        })
        # 终点画在了没涂过的格子上：保存就被挡住，所以直接写文件模拟历史数据
        self.assertFalse(upsert["ok"], upsert)
        from nav_grid_editor.services.maps.links import links_path, upsert_link

        upsert_link(links_path(self.svc.grid2d_dir), {
            "id": "legacy-broken",
            "from": {"map": "map02", "tier": "", "zoom": "4",
                     "rect": {"x0": 0, "z0": 0, "x1": 1, "z1": 1}},
            "to": {"map": "map02", "tier": TIER_A, "zoom": "4",
                   "rect": {"x0": 0, "z0": 0, "x1": 1, "z1": 1}},
        })
        graph = self.svc.nav_graph("map02", "4")
        self.assertFalse(graph["ready"])
        self.assertTrue(
            any("端点无效" in text for text in graph["blockers"]), graph["blockers"])

    def test_unlabelled_xz_overlap_blocks_readiness(self):
        """主图与分层在同一 X/Z 都有格子，但两侧都没标高度 —— 会串楼层。"""
        self._grid("", [[0, 0]])
        self._grid(TIER_A, [[0, 0]])
        graph = self.svc.nav_graph("map02", "4")
        self.assertFalse(graph["ready"], graph)
        self.assertTrue(
            any("重合区" in text and "大地图" in text
                for text in graph["blockers"]), graph["blockers"])

    def test_labelling_both_sides_clears_the_overlap_blocker(self):
        self._grid("", [[0, 0]], self._region(100, 110))
        self._grid(TIER_A, [[0, 0]], self._region(200, 210))
        self._link("", TIER_A)
        graph = self.svc.nav_graph("map02", "4")
        self.assertEqual(graph["blockers"], [])
        self.assertTrue(graph["ready"])

    def test_three_d_overlap_is_not_a_blocker(self):
        """两侧高度区间真的错开（X/Z 与 Y 都重合）时，这处重合是解释得通的。"""
        self._grid("", [[0, 0]], self._region(100, 120))
        self._grid(TIER_A, [[0, 0]], self._region(200, 220))
        graph = self.svc.nav_graph("map02", "4")
        self.assertEqual(graph["blockers"], [])

    def test_main_grid_is_never_punched_out(self):
        """主地图那一格仍然是 Free —— 地表是真能走的，不能为了避重叠挖掉它。"""
        self._grid("", [[0, 0]])
        self._grid(TIER_A, [[0, 0]])
        self.svc.nav_graph("map02", "4")
        main = self.svc.get_grid2d("map02", "4", "")
        self.assertEqual(main["data"]["cells"], [[0, 0]])
        self.assertEqual(main["data"]["blocked"], [])

    def test_summary_drops_cell_coordinates_keeps_the_verdict(self):
        """界面只显示「能不能导航」，没必要为它传几万个坐标。"""
        self._grid("", [[0, 0]])
        self._grid(TIER_A, [[0, 0]])
        full = self.svc.nav_graph("map02", "4")
        brief = self.svc.nav_graph("map02", "4", summary=True)
        self.assertNotIn("free_cells", brief["tiers"][0])
        self.assertTrue(all("free_count" in tier for tier in brief["tiers"]))
        self.assertEqual(
            [tier["free_count"] for tier in brief["tiers"]],
            [len(tier["free_cells"]) for tier in full["tiers"]],
        )
        self.assertEqual(brief["ready"], full["ready"])
        self.assertEqual(brief["blockers"], full["blockers"])
        self.assertEqual(brief["tiers"][0]["grid"], full["tiers"][0]["grid"])

    def test_links_to_other_tiers_travel_with_the_graph(self):
        self._grid("", [[0, 0]])
        self._grid(TIER_A, [[0, 0]], self._region(200, 210))
        self._grid(TIER_B, [[0, 0]], self._region(100, 110))
        self._grid("", [[0, 0]], self._region(300, 310))
        self._link(TIER_A, TIER_B, kind="door")
        graph = self.svc.nav_graph("map02", "4")
        self.assertEqual(len(graph["links"]), 1)
        self.assertEqual(graph["links"][0]["kind"], "door")
        self.assertEqual(graph["links"][0]["from"]["zoom"], "4")


if __name__ == "__main__":
    unittest.main()
