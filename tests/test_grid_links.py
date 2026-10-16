# -*- coding: utf-8 -*-
"""Cross-layer grid-link storage and tiered grid files."""

import tempfile
import unittest
from pathlib import Path

from nav_grid_editor.services.maps import MapService
from nav_grid_editor.services.maps.links import (
    LINK_SCHEMA_VERSION,
    delete_link,
    links_path,
    load_links,
    upsert_link,
)


class GridLinkStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_upsert_and_delete_link(self):
        path = links_path(self.root)
        raw = {
            "from": {
                "map": "map02", "tier": "scene_map02_lv002_layer_tips_4",
                "rect": {"x0": 1, "z0": 2, "x1": 3, "z1": 4},
            },
            "to": {
                "map": "map02", "tier": "",
                "rect": {"x0": 10, "z0": 20, "x1": 11, "z1": 21},
            },
            "kind": "stairs",
            "bidirectional": True,
            "cost": 4,
        }
        link = upsert_link(path, raw)
        self.assertTrue(link["id"])
        self.assertEqual(load_links(path)["links"][0]["kind"], "stairs")
        self.assertTrue(delete_link(path, link["id"]))
        self.assertEqual(load_links(path)["links"], [])

    def test_invalid_rect_is_rejected(self):
        with self.assertRaises(ValueError):
            upsert_link(links_path(self.root), {
                "from": {
                    "map": "map02", "tier": "",
                    "rect": {"x0": 2, "z0": 0, "x1": 1, "z1": 3},
                },
                "to": {
                    "map": "map01", "tier": "",
                    "rect": {"x0": 4, "z0": 4, "x1": 5, "z1": 5},
                },
            })

    def test_rope_link_kind_is_accepted(self):
        link = upsert_link(links_path(self.root), {
            "from": {
                "map": "map02", "tier": "scene_map02_lv005_layer_tips_7",
                "rect": {"x0": 0, "z0": 0, "x1": 1, "z1": 1},
            },
            "to": {
                "map": "map02", "tier": "scene_map02_lv005_layer_tips_6",
                "rect": {"x0": 2, "z0": 2, "x1": 3, "z1": 3},
            },
            "kind": "rope",
            "bidirectional": False,
            "cost": 2,
        })
        self.assertEqual(link["kind"], "rope")

    def test_endpoint_zoom_is_stored_and_validated(self):
        path = links_path(self.root)
        raw = {
            "from": {
                "map": "map02", "tier": "", "zoom": "4",
                "rect": {"x0": 0, "z0": 0, "x1": 1, "z1": 1},
            },
            "to": {
                "map": "map02", "tier": "", "zoom": "4",
                "rect": {"x0": 2, "z0": 2, "x1": 3, "z1": 3},
            },
        }
        link = upsert_link(path, raw)
        self.assertEqual(link["from"]["zoom"], "4")
        self.assertEqual(load_links(path)["links"][0]["to"]["zoom"], "4")
        with self.assertRaises(ValueError):
            upsert_link(path, {
                **raw,
                "from": {**raw["from"], "zoom": "../4"},
            })

    def test_v1_file_loads_with_zoom_filled_in(self):
        """v1 的端点没有 zoom：读出来要补齐，形状和当前 schema 一致。"""
        path = links_path(self.root)
        path.write_text(
            '{"schema_version": 1, "links": [{"id": "a", "from": '
            '{"map": "map02", "tier": "", '
            '"rect": {"x0": 0, "z0": 0, "x1": 1, "z1": 1}}, "to": '
            '{"map": "map02", "tier": "", '
            '"rect": {"x0": 2, "z0": 2, "x1": 3, "z1": 3}}}]}',
            encoding="utf-8",
        )
        data = load_links(path)
        self.assertEqual(data["schema_version"], LINK_SCHEMA_VERSION)
        self.assertEqual(data["links"][0]["from"]["zoom"], "")
        self.assertEqual(data["links"][0]["to"]["zoom"], "")
        # v3 的端点高度是**可选**的：老数据没有就保持缺失，"不知道"不能被编出来
        self.assertNotIn("y_min", data["links"][0]["from"])
        self.assertNotIn("y_max", data["links"][0]["to"])

    def test_endpoint_y_range_is_optional_but_validated(self):
        """端点高度（v3）：两个键要么都给要么都不给，且必须有限、有序。"""
        path = links_path(self.root)
        base = {
            "from": {"map": "map02", "tier": "", "zoom": "4",
                     "rect": {"x0": 0, "z0": 0, "x1": 1, "z1": 1}},
            "to": {"map": "map02", "tier": "tier_a", "zoom": "4",
                   "rect": {"x0": 2, "z0": 2, "x1": 3, "z1": 3}},
        }
        # 不填 → 端点里就没有这两个键
        link = upsert_link(path, dict(base))
        self.assertNotIn("y_min", link["from"])
        # 都填 → 原样保留
        link = upsert_link(path, {
            **base,
            "from": {**base["from"], "y_min": 249, "y_max": 251},
            "to": {**base["to"], "y_min": 239, "y_max": 241},
        })
        self.assertEqual(link["from"]["y_min"], 249.0)
        self.assertEqual(link["to"]["y_max"], 241.0)
        # 只给一个 / 逆序 / 非有限 → 拒绝
        for label, extra in (("只给 y_min", {"y_min": 1}),
                             ("逆序", {"y_min": 9, "y_max": 1}),
                             ("非有限", {"y_min": float("nan"), "y_max": 1})):
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    upsert_link(path, {
                        **base,
                        "from": {**base["from"], **extra},
                    })


class LinkEndpointValidationTest(unittest.TestCase):
    """端点必须落在这层的可走格上 —— 悬空的连接是通向虚空的转移。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.svc = MapService(
            data_root=self.root,
            tiles_root=self.root / "tiles",
            profile_dir=self.root / "profile",
            grid2d_dir=self.root / "grids",
        )
        self._save_main_grid()
        self._save_tier_grid("scene_map02_lv005_layer_tips_7")

    def tearDown(self):
        self._tmp.cleanup()

    def _save_main_grid(self):
        self.svc.save_grid2d("map02", "4", {
            "origin": [0, 0, 0], "cell_size": 1,
            "shape": [10, 10], "cells": [[0, 0], [1, 1]], "blocked": [],
        }, tier="")

    def _save_tier_grid(self, tier):
        self.svc.save_grid2d("map02", "4", {
            "origin": [100, 0, 100], "cell_size": 1,
            "shape": [10, 10], "cells": [[0, 0], [1, 1]], "blocked": [],
        }, tier=tier)

    def _link(self, from_rect, to_rect):
        return {
            "from": {"map": "map02", "tier": "", "zoom": "4", "rect": from_rect},
            "to": {
                "map": "map02",
                "tier": "scene_map02_lv005_layer_tips_7",
                "zoom": "4",
                "rect": to_rect,
            },
        }

    def test_endpoint_on_free_cell_is_accepted(self):
        result = self.svc.save_grid_link(
            self._link({"x0": 0, "z0": 0, "x1": 1, "z1": 1},
                       {"x0": 101, "z0": 101, "x1": 102, "z1": 102}))
        self.assertTrue(result["ok"], result)

    def test_endpoint_on_unknown_cell_is_rejected(self):
        result = self.svc.save_grid_link(
            self._link({"x0": 5, "z0": 5, "x1": 6, "z1": 6},
                       {"x0": 101, "z0": 101, "x1": 102, "z1": 102}))
        self.assertFalse(result["ok"])
        self.assertIn("没有可走格", result["error"])
        self.assertIn("起点", result["error"])

    def test_endpoint_outside_grid_is_rejected(self):
        """终点整块落在网格范围外（真实数据里踩过：z 比 origin 小一格）。"""
        result = self.svc.save_grid_link(
            self._link({"x0": 0, "z0": 0, "x1": 1, "z1": 1},
                       {"x0": 101, "z0": 90, "x1": 102, "z1": 91}))
        self.assertFalse(result["ok"])
        self.assertIn("范围之外", result["error"])

    def test_missing_grid_is_rejected(self):
        rejected = self.svc.save_grid_link({
            "from": {"map": "map02", "tier": "", "zoom": "4",
                     "rect": {"x0": 0, "z0": 0, "x1": 1, "z1": 1}},
            "to": {"map": "map02", "tier": "scene_map02_lv002_layer_tips_4",
                   "zoom": "4",
                   "rect": {"x0": 0, "z0": 0, "x1": 1, "z1": 1}},
        })
        self.assertFalse(rejected["ok"])
        self.assertIn("网格不存在", rejected["error"])

    def test_read_reports_broken_links_without_changing_them(self):
        """文件里已有的坏连接要读得出来、标得出来，但不能被悄悄改掉。"""
        upsert_link(links_path(self.root / "grids"), {
            "id": "broken",
            "from": {"map": "map02", "tier": "", "zoom": "4",
                     "rect": {"x0": 5, "z0": 5, "x1": 6, "z1": 6}},
            "to": {"map": "map02", "tier": "", "zoom": "4",
                   "rect": {"x0": 0, "z0": 0, "x1": 1, "z1": 1}},
        })
        links = self.svc.get_grid_links("map02", "4")["links"]
        self.assertEqual(len(links), 1)
        self.assertFalse(links[0]["checks"]["valid"])
        self.assertIn("没有可走格", links[0]["checks"]["issue"])
        self.assertEqual(links[0]["checks"]["to"]["free"], 1)
        on_disk = load_links(links_path(self.root / "grids"))["links"]
        self.assertNotIn("checks", on_disk[0])

    def test_drop_is_computed_only_when_both_ends_carry_y(self):
        """落差是**量出来的**：两端都标了高度才算，否则 None。

        从 floor_regions 区间去推落差是另一回事（区间不是点），所以少一端就明确不
        给，不拿推断值凑。
        """
        def save(extra_from, extra_to, link_id):
            saved = self.svc.save_grid_link({
                "id": link_id,
                "from": {"map": "map02", "tier": "", "zoom": "4",
                         "rect": {"x0": 0, "z0": 0, "x1": 1, "z1": 1},
                         **extra_from},
                "to": {"map": "map02",
                       "tier": "scene_map02_lv005_layer_tips_7",
                       "zoom": "4",
                       "rect": {"x0": 101, "z0": 101, "x1": 102, "z1": 102},
                       **extra_to},
            })
            self.assertTrue(saved["ok"], saved)
            return saved["link"]["id"]

        both = save({"y_min": 300, "y_max": 302},
                    {"y_min": 250, "y_max": 252}, "both")
        only_from = save({"y_min": 300, "y_max": 302}, {}, "only-from")
        neither = save({}, {}, "neither")

        by_id = {item["id"]: item for item in
                 self.svc.get_grid_links("map02", "4")["links"]}
        drop = by_id[both]["checks"]["drop"]
        self.assertIsNotNone(drop)
        self.assertAlmostEqual(drop["from_center"], 301.0)
        self.assertAlmostEqual(drop["to_center"], 251.0)
        self.assertAlmostEqual(drop["drop"], -50.0)
        self.assertAlmostEqual(drop["from_span"], 2.0)
        self.assertIsNone(by_id[only_from]["checks"]["drop"])
        self.assertIsNone(by_id[neither]["checks"]["drop"])
        # Y 也会出现在端点校验结果里，供界面显示
        self.assertEqual(by_id[both]["checks"]["from"]["y_min"], 300.0)
        self.assertIsNone(by_id[neither]["checks"]["from"]["y_min"])

    def test_legacy_endpoint_without_zoom_resolves_by_query_zoom(self):
        """v1 端点没写 zoom：按查询的 zoom 解析，不能拿别的档次顶替。"""
        upsert_link(links_path(self.root / "grids"), {
            "id": "legacy",
            "from": {"map": "map02", "tier": "",
                     "rect": {"x0": 0, "z0": 0, "x1": 1, "z1": 1}},
            "to": {"map": "map02", "tier": "scene_map02_lv005_layer_tips_7",
                   "rect": {"x0": 101, "z0": 101, "x1": 102, "z1": 102}},
        })
        at4 = self.svc.get_grid_links("map02", "4")["links"]
        self.assertEqual(len(at4), 1)
        self.assertTrue(at4[0]["checks"]["valid"], at4[0]["checks"])
        # 换个 zoom 查询没有任何网格，端点必须报出来而不是沿用 zoom 4 的
        at3 = self.svc.get_grid_links("map02", "3")["links"]
        self.assertEqual(len(at3), 1)
        self.assertFalse(at3[0]["checks"]["valid"])
        self.assertIn("网格不存在", at3[0]["checks"]["issue"])


class TieredGridServiceTest(unittest.TestCase):
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

    def test_tiered_grid_roundtrip_uses_distinct_file(self):
        data = {
            "origin": [0, 0, 0],
            "cell_size": 1,
            "cells": [[1, 1]],
            "blocked": [],
        }
        result = self.svc.save_grid2d(
            "map02", "4", data, tier="scene_map02_lv002_layer_tips_4",
        )
        self.assertTrue(result["ok"], result)
        self.assertIn("scene_map02_lv002_layer_tips_4", result["path"])
        loaded = self.svc.get_grid2d(
            "map02", "4", "scene_map02_lv002_layer_tips_4",
        )
        self.assertEqual(loaded["data"]["cells"], [[0, 0]])


if __name__ == "__main__":
    unittest.main()
