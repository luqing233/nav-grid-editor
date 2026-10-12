# -*- coding: utf-8 -*-
"""层声明索引（`nav-grid-data/levels.json`）。

它存在的原因：分层目录 `catalog.json` 是 `fetch-layers` 的可重生本地缓存、**不进
版本库**，所以消费侧拿不到「哪个 level_id 声明了哪些层」。只能按网格文件反推，而那样
会把不同 level_id 的层混成一批 —— map02 的目录里有 9 个 level_id，只要有人给 lv002
画一张网格，按文件发现就会把 lv002 与 lv005 的候选混在一起，对跟随器是静默错层。

这份索引把该声明物化进数据仓库，于是消费侧只读网格目录就够。
"""

import json
import tempfile
import unittest
from pathlib import Path

from nav_grid_editor.services.maps import MapService
from nav_grid_editor.services.maps.layers import (
    LEVEL_INDEX_FILENAME,
    build_level_index,
    levels_index_path,
    load_level_index,
)


class BuildLevelIndexTest(unittest.TestCase):
    def test_keeps_declared_tiers_without_grids_and_only_public_fields(self):
        """必须保留**还没有网格文件**的层 —— 消费侧靠它区分「声明了但没网格」。"""
        doc = build_level_index([
            {"id": "tier_b", "map_name": "map02", "level_id": "lv1",
             "name": "B", "group": "G", "map_category": "layered",
             # 下面这些是抓取用的，索引里不该出现
             "bounds": {"west": 0}, "path": "map02/lv1/b.png",
             "source_url": "https://x", "entrances": [{"x": 1}],
             "width": 100, "height": 100, "filename": "b.png"},
            {"id": "tier_a", "map_name": "map01", "level_id": "lv2", "name": "A"},
        ], generated_at="2026-01-01T00:00:00+00:00")
        self.assertEqual(doc["schema_version"], 1)
        self.assertEqual(doc["generated_at"], "2026-01-01T00:00:00+00:00")
        # 按 (map_name, level_id, id) 排序
        self.assertEqual([item["id"] for item in doc["layers"]],
                         ["tier_a", "tier_b"])
        entry = doc["layers"][1]
        self.assertEqual(sorted(entry), ["group", "id", "level_id",
                                         "map_category", "map_name", "name"])
        for noisy in ("bounds", "path", "source_url", "entrances", "width"):
            self.assertNotIn(noisy, entry)

    def test_missing_fields_are_normalized(self):
        doc = build_level_index([{"id": "tier_a"}])
        self.assertEqual(doc["layers"][0]["map_name"], "")
        self.assertEqual(doc["layers"][0]["level_id"], "")

    def test_entries_without_id_are_dropped(self):
        doc = build_level_index([{"map_name": "map02"}, {"id": "tier_a"}])
        self.assertEqual([item["id"] for item in doc["layers"]], ["tier_a"])

    def test_empty_input_is_still_a_valid_document(self):
        doc = build_level_index([])
        self.assertEqual(doc["layers"], [])
        self.assertTrue(doc["note"])


class LevelIndexServiceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.tiles = self.root / "tiles"
        self.grids = self.root / "grids"
        self.svc = MapService(
            data_root=self.root, tiles_root=self.tiles,
            profile_dir=self.root / "p", grid2d_dir=self.grids,
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _write_catalog(self, layers):
        d = self.tiles / "layers"
        d.mkdir(parents=True, exist_ok=True)
        (d / "catalog.json").write_text(
            json.dumps({"layers": layers}, ensure_ascii=False), encoding="utf-8")

    def test_refuses_to_write_without_a_catalog(self):
        """没有分层目录时**不能写** —— 那时 _layer_entries() 会退回内置兜底目录
        （只有 7 条），拿它覆盖已提交的完整索引就是数据丢失。"""
        result = self.svc.sync_level_index()
        self.assertFalse(result["ok"])
        self.assertIn("分层目录", result["error"])
        self.assertFalse((self.grids / LEVEL_INDEX_FILENAME).exists())

    def test_writes_then_skips_when_unchanged(self):
        self._write_catalog([
            {"id": "tier_a", "map_name": "map02", "level_id": "lv1"},
            {"id": "tier_b", "map_name": "map02", "level_id": "lv1"},
        ])
        first = self.svc.sync_level_index()
        self.assertTrue(first["ok"], first)
        self.assertFalse(first["unchanged"])
        path = self.grids / LEVEL_INDEX_FILENAME
        self.assertTrue(path.is_file())
        stamp = json.loads(path.read_text(encoding="utf-8"))["generated_at"]

        # 内容没变就不重写：这份文件在版本库里，每次保存都刷时间戳会一直脏
        second = self.svc.sync_level_index()
        self.assertTrue(second["ok"])
        self.assertTrue(second["unchanged"])
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8"))["generated_at"], stamp)

        # 目录变了才重写
        self._write_catalog([
            {"id": "tier_a", "map_name": "map02", "level_id": "lv1"},
            {"id": "tier_b", "map_name": "map02", "level_id": "lv1"},
            {"id": "tier_c", "map_name": "map02", "level_id": "lv2"},
        ])
        third = self.svc.sync_level_index()
        self.assertFalse(third["unchanged"])
        self.assertEqual(third["count"], 3)

    def test_save_grid_triggers_the_index(self):
        """画了网格就说明这一层存在 —— 索引要跟着出现，否则消费侧枚举不到。"""
        self._write_catalog([
            {"id": "tier_a", "map_name": "map02", "level_id": "lv1"},
        ])
        self.assertFalse((self.grids / LEVEL_INDEX_FILENAME).exists())
        saved = self.svc.save_grid2d("map02", "4", {
            "origin": [0, 0, 0], "cell_size": 1, "shape": [2, 2],
            "cells": [[0, 0]], "blocked": [],
        }, tier="tier_a")
        self.assertTrue(saved["ok"], saved)
        doc = load_level_index(self.grids / LEVEL_INDEX_FILENAME)
        self.assertIsNotNone(doc)
        self.assertEqual([item["id"] for item in doc["layers"]], ["tier_a"])

    def test_broken_catalog_does_not_break_saving(self):
        """索引刷新失败不该让网格保存失败。"""
        d = self.tiles / "layers"
        d.mkdir(parents=True, exist_ok=True)
        (d / "catalog.json").write_text("{not json", encoding="utf-8")
        saved = self.svc.save_grid2d("map02", "4", {
            "origin": [0, 0, 0], "cell_size": 1, "shape": [2, 2],
            "cells": [[0, 0]], "blocked": [],
        }, tier="tier_a")
        self.assertTrue(saved["ok"], saved)
        self.assertFalse((self.grids / LEVEL_INDEX_FILENAME).exists())

    def test_read_falls_back_to_computing_without_writing(self):
        self._write_catalog([
            {"id": "tier_a", "map_name": "map02", "level_id": "lv1"},
        ])
        doc = self.svc.get_level_index()
        self.assertEqual([item["id"] for item in doc["layers"]], ["tier_a"])
        self.assertFalse((self.grids / LEVEL_INDEX_FILENAME).exists())

    def test_index_path_sits_next_to_links(self):
        self.assertEqual(levels_index_path(self.grids),
                         self.grids / "levels.json")


if __name__ == "__main__":
    unittest.main()
