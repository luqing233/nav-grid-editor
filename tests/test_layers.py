# -*- coding: utf-8 -*-
"""Official layered-map catalog and cache tests."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nav_grid_editor.services.maps.layers import (
    find_layer,
    layer_cache_relpath,
    layer_source_url,
    load_layer_catalog,
    list_layers,
)
from nav_grid_editor.services.maps.io import solid_png
from nav_grid_editor.services.maps.service import MapService
from nav_grid_editor.commands import fetch_layers
from nav_grid_editor.commands.fetch_layers import (
    MAP_DISPLAY_NAMES,
    _extract_mapping,
    audit_entrance_markers,
    collect_layer_entries,
)


class MapDisplayNameTest(unittest.TestCase):
    """地图显示名要和客户端文本表一致——旧写法在客户端里根本不存在。"""

    def test_map01_matches_client_text(self):
        # I18nTextTable_CN 里「四号谷地」760 条、「四号谷底」0 条
        self.assertEqual(MAP_DISPLAY_NAMES["map01"], "四号谷地")
        self.assertEqual(MAP_DISPLAY_NAMES["map02"], "武陵")

    def test_stale_spelling_is_not_reintroduced(self):
        repo_root = Path(fetch_layers.__file__).resolve().parents[3]
        targets = [
            repo_root / "README.md",
            *sorted((repo_root / "src").rglob("*.py")),
            *sorted((repo_root / "src").rglob("*.js")),
        ]
        stale = [
            str(path.relative_to(repo_root))
            for path in targets
            if path.is_file() and "四号谷底" in path.read_text(encoding="utf-8", errors="ignore")
        ]
        self.assertEqual(stale, [], "客户端文本表里没有「四号谷底」，别把这个写法写回来")


class LayerCatalogTest(unittest.TestCase):
    def test_map02_catalog_contains_official_tiers(self):
        layers = list_layers("map02", "4")
        self.assertEqual(len(layers), 7)
        self.assertEqual(
            [layer["name"] for layer in layers[:3]],
            ["储备站二层", "储备站一层", "储备站地下室"],
        )
        self.assertEqual(
            [layer["id"] for layer in layers[3:]],
            [
                "scene_map02_lv002_layer_tips_4",
                "scene_map02_lv002_layer_tips_5",
                "scene_map02_lv002_layer_tips_6",
                "scene_map02_lv002_layer_tips_7",
            ],
        )
        self.assertEqual(
            layers[3]["bounds"],
            {"west": -1664.0, "east": -512.0,
             "south": -1024.0, "north": 384.0},
        )
        self.assertEqual(layers[3]["group"], "天井院")
        self.assertEqual(layers[3]["group_key"], "map02_lv002:19")
        self.assertEqual(layers[0]["group_key"], "map02_lv002:18")

    def test_builtin_seed_matches_generated_catalog_schema(self):
        """内置兜底必须和 fetch-layers 生成的是同一套字段。

        两者取 id 的口径曾经不同（内置用图标名 tier_02_002_4、生成用
        logicId），于是没有 catalog.json 时列出的层根本对不上网格文件名，
        楼层解析只能一路退化成「大地图」。
        """
        for layer in list_layers("map02", "4"):
            with self.subTest(layer=layer["id"]):
                self.assertEqual(layer["id"], layer["logic_id"])
                self.assertTrue(layer["id"].startswith("scene_"))
                self.assertEqual(
                    layer["path"],
                    f"map02/{layer['level_id']}/{layer['id']}.png",
                )
                self.assertEqual(layer["filename"], f"{layer['id']}.png")
                self.assertTrue(layer["level_id"])
                self.assertGreater(layer["width"], 0)
                self.assertGreater(layer["height"], 0)
                # 入口图标按半径 28 的标记点匹配，几十像素的误差就切不动楼层
                self.assertTrue(layer["entrances"])
                self.assertEqual(layer["entrance"], layer["entrances"][0])
                self.assertLessEqual(layer["entrance"]["radius"], 50.0)

    def test_unknown_map_or_layer_returns_none(self):
        self.assertEqual(list_layers("map01", "4"), [])
        self.assertIsNone(find_layer("map02", "4", "missing"))
        self.assertIsNone(find_layer("../map02", "4", "tier_02_002_4"))

    def test_source_url_and_cache_path_are_from_catalog(self):
        layer = find_layer("map02", "4", "scene_map02_lv002_layer_tips_4")
        self.assertIsNotNone(layer)
        self.assertEqual(
            layer_source_url(layer),
            "https://assets.skland.com/game-map/endfield/"
            "region_1_2/Map02Lv002/tier_02_002_4.png",
        )
        self.assertEqual(
            layer_cache_relpath(layer),
            Path("layers") / "map02" / "map02_lv002"
            / "scene_map02_lv002_layer_tips_4.png",
        )

    def test_cache_path_falls_back_without_explicit_path(self):
        """手写的 catalog 不带 path/icon 时，回退也要落在同一套命名上。"""
        self.assertEqual(
            layer_cache_relpath({
                "id": "scene_x", "map_name": "m", "level_id": "lv",
            }),
            Path("layers") / "m" / "lv" / "scene_x.png",
        )
        self.assertEqual(
            layer_source_url({
                "id": "scene_x", "icon": "MapA/tier_1",
                "region_version": "1_2",
            }),
            "https://assets.skland.com/game-map/endfield/"
            "region_1_2/MapA/tier_1.png",
        )
        with self.assertRaises(ValueError):
            layer_source_url({"id": "scene_x"})

    def test_generated_catalog_is_loaded_as_layer_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.json"
            path.write_text(
                '{"layers":[{"id":"x","map_name":"m","bounds":{},'
                '"entrance":{}}]}',
                encoding="utf-8",
            )
            entries = load_layer_catalog(path)
        self.assertEqual(entries[0]["id"], "x")
        self.assertEqual(list_layers("m", "4", entries)[0]["id"], "x")


class FetchLayerParsingTest(unittest.TestCase):
    def test_js_mapping_extraction(self):
        text = (
            'var Zy={scene_a:{icon:"MapA/tier_1",'
            'bounds:[{lng:-1,lat:-2},{lng:3,lat:4}]},'
            'scene_b:{icon:"MapB/tier_2",'
            'bounds:[{lng:5,lat:6},{lng:7,lat:8}]}};'
        )
        mapping = _extract_mapping(text)
        self.assertEqual(list(mapping), ["scene_a", "scene_b"])
        self.assertEqual(mapping["scene_a"]["icon"], "MapA/tier_1")

    def test_collect_entries_uses_logic_id_mapping_and_sublevel_anchor(self):
        tree = {"maps": [{
            "id": "map02",
            "name": "武陵",
            "levels": [{
                "id": "map02_lv002",
                "name": "武陵城",
                "subLevels": [{"name": "天井院", "pos": {"x": -819, "y": -527}}],
                "regionSets": [{
                    "id": 19,
                    "regions": [
                        {"id": 259, "name": "天井院地下一层",
                         "tierIndex": -1,
                         "logicId": "scene_map02_lv002_layer_tips_4"},
                    ],
                }],
            }],
        }]}
        mapping = {
            "scene_map02_lv002_layer_tips_4": {
                "icon": "Map02Lv002/tier_02_002_4",
                "bounds": [{"lng": -1664, "lat": -1024},
                           {"lng": -512, "lat": 384}],
            },
        }
        entries, missing = collect_layer_entries(
            tree, mapping, {"map02": "1_2"}, [], {}
        )
        self.assertEqual(missing, [])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["group"], "天井院")
        self.assertEqual(entries[0]["entrance"]["x"], -819.0)
        self.assertIn("region_1_2/Map02Lv002", entries[0]["source_url"])

    def test_exact_target_region_id_attaches_all_entrance_markers(self):
        tree = {"maps": [{
            "id": "map02",
            "name": "武陵",
            "levels": [{
                "id": "map02_lv002",
                "name": "武陵城",
                "subLevels": [],
                "regionSets": [{
                    "id": 19,
                    "regions": [{
                        "id": 259,
                        "name": "天井院地下一层",
                        "tierIndex": -1,
                        "logicId": "scene_map02_lv002_layer_tips_4",
                    }],
                }],
            }],
        }]}
        mapping = {
            "scene_map02_lv002_layer_tips_4": {
                "icon": "Map02Lv002/tier_02_002_4",
                "bounds": [{"lng": -1664, "lat": -1024},
                           {"lng": -512, "lat": 384}],
            },
        }
        markers = {"map02": [
            {"templateId": "feec2fbd748b3d10131a07971b25baf2",
             "levelId": "map02_lv002", "targetRegionId": 19,
             "pos": {"x": -800, "z": -536, "y": 0}},
            {"templateId": "feec2fbd748b3d10131a07971b25baf2",
             "levelId": "map02_lv002", "targetRegionId": 19,
             "pos": {"x": -752, "z": -534, "y": 0}},
        ]}
        entries, missing = collect_layer_entries(
            tree, mapping, {"map02": "1_2"}, [], markers
        )
        self.assertEqual(missing, [])
        self.assertEqual(
            [(item["x"], item["z"]) for item in entries[0]["entrances"]],
            [(-800.0, -536.0), (-752.0, -534.0)],
        )
        self.assertEqual(
            audit_entrance_markers(markers, entries)["matched"],
            2,
        )

    def test_group_without_entrance_marker_is_independent_map(self):
        tree = {"maps": [{
            "id": "map02",
            "name": "武陵",
            "levels": [{
                "id": "indie_dg007",
                "name": "首墩内部",
                "subLevels": [],
                "regionSets": [{
                    "id": 28,
                    "regions": [{
                        "id": 335,
                        "name": "首墩顶层",
                        "tierIndex": 4,
                        "logicId": "scene_indie_dg007_layer_tips_4",
                    }],
                }],
            }],
        }]}
        mapping = {
            "scene_indie_dg007_layer_tips_4": {
                "icon": "IndieDg007/top",
                "bounds": [{"lng": -512, "lat": -512},
                           {"lng": 512, "lat": 512}],
            },
        }
        entries, _missing = collect_layer_entries(
            tree, mapping, {"indie_dg007": "1_2"}, [], {"map02": []}
        )
        self.assertEqual(entries[0]["map_category"], "independent")
        self.assertEqual(entries[0]["group"], "首墩内部")


class LayerCacheTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.svc = MapService(
            data_root=root,
            tiles_root=root / "tiles",
            profile_dir=root / "profile",
            grid2d_dir=root / "grids",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_layer_image_is_downloaded_once_then_served_from_cache(self):
        png = solid_png(1, 1, (0, 0, 0))
        layer_id = "scene_map02_lv002_layer_tips_4"
        with mock.patch(
            "nav_grid_editor.services.maps.service._download_layer_image",
            return_value=png,
        ) as download:
            first = self.svc.get_layer_image("map02", "4", layer_id)
            second = self.svc.get_layer_image("map02", "4", layer_id)

        self.assertEqual(first, png)
        self.assertEqual(second, png)
        self.assertEqual(download.call_count, 1)
        # 缓存位置与 fetch-layers 写的是同一个文件，两边不能各存一份。
        self.assertTrue(
            (self.svc.store.tiles_root / "layers" / "map02" / "map02_lv002"
             / f"{layer_id}.png").is_file()
        )

    def test_generated_catalog_is_used_by_service(self):
        layer_dir = self.svc.store.tiles_root / "layers" / "m" / "lv"
        layer_dir.mkdir(parents=True)
        png_path = layer_dir / "layer.png"
        png = solid_png(1, 1, (1, 2, 3))
        png_path.write_bytes(png)
        catalog = self.svc.store.tiles_root / "layers" / "catalog.json"
        catalog.write_text(json.dumps({
            "layers": [{
                "id": "layer",
                "map_name": "m",
                "path": "m/lv/layer.png",
                "bounds": {"west": 0, "east": 1, "south": 0, "north": 1},
                "entrance": {},
            }],
        }), encoding="utf-8")

        self.assertEqual(
            [item["id"] for item in self.svc.list_layers("m", "4")],
            ["layer"],
        )
        self.assertEqual(
            self.svc.get_layer_image("m", "4", "layer"),
            png,
        )


if __name__ == "__main__":
    unittest.main()
