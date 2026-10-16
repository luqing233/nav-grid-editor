# -*- coding: utf-8 -*-
"""官方地图 WS 位置客户端：位置解析与生命周期。"""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from nav_grid_editor.services import marks as marks_mod
from nav_grid_editor.services.endfield_ws import EndfieldPositionClient, extract_position
from nav_grid_editor.services.maps import MapService


class ExtractPositionTest(unittest.TestCase):
    def test_nested_pos_with_map_id(self):
        payload = {"type": 1012, "data": {"pos": {"x": 1.5, "y": 2.0, "z": -3.25},
                                          "mapId": "map01", "levelId": "lv1"}}
        pos, map_id, x, y, z = extract_position(payload)
        self.assertEqual(pos, payload["data"]["pos"])
        self.assertEqual(map_id, "map01")
        self.assertEqual((x, y, z), (1.5, 2.0, -3.25))

    def test_flat_xyz_in_data(self):
        payload = {"data": {"x": 1, "y": 2, "z": 3, "levelId": "lv9"}}
        _pos, map_id, x, y, z = extract_position(payload)
        self.assertEqual(map_id, "lv9")
        self.assertEqual((x, y, z), (1.0, 2.0, 3.0))

    def test_top_level_xyz(self):
        _pos, map_id, x, y, z = extract_position({"x": 4, "y": 5, "z": 6, "mapId": "m"})
        self.assertEqual(map_id, "m")
        self.assertEqual((x, y, z), (4.0, 5.0, 6.0))

    def test_missing_parts_are_rejected(self):
        for payload in ({}, {"data": {"pos": {"x": 1, "y": 2}}},
                        {"x": 1, "y": 2, "z": 3}, {"data": {"pos": {"x": 1, "y": 2, "z": 3}}}):
            with self.subTest(payload=payload):
                pos, map_id, *_ = extract_position(payload)
                self.assertIsNone(pos)
                self.assertIsNone(map_id)


class ClientLifecycleTest(unittest.TestCase):
    def test_status_before_start_is_idle(self):
        client = EndfieldPositionClient(on_position=lambda _d: None,
                                        content_reader=lambda: "")
        st = client.status()
        self.assertFalse(st["running"])
        self.assertFalse(st["connected"])
        self.assertIsNone(st["last_position"])

    def test_start_and_stop_without_credential_does_not_raise(self):
        client = EndfieldPositionClient(on_position=lambda _d: None,
                                        content_reader=lambda: "")
        res = client.start()
        self.assertTrue(res["ok"])
        self.assertTrue(res["started"])
        self.assertTrue(client.status()["running"])
        client.stop()
        self.assertFalse(client.status()["running"])

    def test_position_snapshot_keeps_level_id(self):
        seen = []
        client = EndfieldPositionClient(on_position=seen.append,
                                        content_reader=lambda: "")
        client._handle_position({
            "type": 1012,
            "data": {
                "pos": {"x": -382.4, "y": 262.3, "z": -478.5},
                "mapId": "map02",
                "levelId": "map02_lv005",
                "isOnline": True,
            },
        })
        st = client.status()
        self.assertEqual(st["map_id"], "map02")
        self.assertEqual(st["level_id"], "map02_lv005")
        self.assertEqual(st["last_position"]["mapId"], "map02")
        self.assertEqual(st["last_position"]["levelId"], "map02_lv005")
        self.assertEqual(seen[0]["levelId"], "map02_lv005")

    def test_position_log_contains_complete_raw_message(self):
        logs = []
        client = EndfieldPositionClient(on_position=lambda _d: None,
                                        log=logs.append,
                                        content_reader=lambda: "")
        payload = {
            "type": 1012,
            "data": {
                "pos": {"x": -382.4, "y": 262.3, "z": -478.5},
                "levelId": "map02_lv005",
                "isOnline": True,
                "mapId": "map02",
            },
        }
        client._handle_position(payload)
        self.assertEqual(len(logs), 1)
        self.assertIn('"type":1012', logs[0])
        self.assertIn('"levelId":"map02_lv005"', logs[0])
        self.assertIn('"isOnline":true', logs[0])

    def test_auth_failure_is_reflected_in_status(self):
        """认证失败要写进状态：之前 _set_state 写错属性名，状态一直停在初始值。"""
        client = EndfieldPositionClient(on_position=lambda _d: None,
                                        content_reader=lambda: "fake-content")
        with mock.patch.object(marks_mod, "exchange_content",
                               side_effect=RuntimeError("模拟认证失败")):
            client.start()
            deadline = time.time() + 5
            while time.time() < deadline and not client.status()["last_error"]:
                time.sleep(0.02)
            st = client.status()
            client.stop()
        self.assertTrue(st["credential_ready"])
        self.assertIn("模拟认证失败", st["last_error"])
        self.assertFalse(st["connected"])

    def test_repeated_identical_failure_is_logged_once(self):
        logs = []
        client = EndfieldPositionClient(on_position=lambda _d: None, log=logs.append,
                                        content_reader=lambda: "fake-content")
        with mock.patch.object(marks_mod, "exchange_content",
                               side_effect=RuntimeError("同样的错误")), \
                mock.patch("nav_grid_editor.services.endfield_ws._RECONNECT_SECONDS", 0.05):
            client.start()
            time.sleep(0.4)
            client.stop()
        retry_logs = [m for m in logs if "连接中断" in m]
        self.assertEqual(len(retry_logs), 1, logs)


class MapServicePositionTest(unittest.TestCase):
    def test_position_is_broadcast_as_coords_event(self):
        """官方 WS 收到位置后，网页那边的 SSE coords 事件要带上 pos/mapId。"""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            svc = MapService(data_root=root, tiles_root=root / "t",
                             profile_dir=root / "p", grid2d_dir=root / "g")
            q = svc.bus.subscribe()
            try:
                svc._on_position({"pos": {"x": 1.0, "y": 2.0, "z": 3.0}, "mapId": "map01"})
                evt = json.loads(q.get(timeout=1.0))
            finally:
                svc.bus.unsubscribe(q)
        self.assertEqual(evt["type"], "coords")
        self.assertEqual(evt["data"]["mapId"], "map01")
        self.assertEqual(evt["data"]["pos"]["z"], 3.0)

    def test_status_includes_position_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            svc = MapService(data_root=root, tiles_root=root / "t",
                             profile_dir=root / "p", grid2d_dir=root / "g")
            st = svc.status()["position"]
        self.assertFalse(st["running"])
        self.assertFalse(st["connected"])
        # 默认位置源是抓包后端；官方 WS 只剩 NAV_POSITION_SOURCE=ws 的可选兜底。
        self.assertEqual(st["source"], "capture")
        self.assertIn("hint", st)

    def test_ws_source_can_still_be_selected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            svc = MapService(data_root=root, tiles_root=root / "t",
                             profile_dir=root / "p", grid2d_dir=root / "g",
                             position_source="ws")
            st = svc.status()["position"]
        self.assertEqual(st["source"], "ws")
        self.assertIn("credential_ready", st)


if __name__ == "__main__":
    unittest.main()
