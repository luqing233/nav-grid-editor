# -*- coding: utf-8 -*-
"""Cross-layer grid-link storage and tiered grid files."""

import tempfile
import unittest
from pathlib import Path

from nav_grid_editor.services.maps import MapService
from nav_grid_editor.services.maps.links import (
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
