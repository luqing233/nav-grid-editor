# -*- coding: utf-8 -*-
"""抓包后端位置源：陈旧度、flows 解析、字段映射与轮询生命周期。"""

import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from nav_grid_editor.services.capture_position import (
    CAPTURE_PORT_END,
    CAPTURE_PORT_START,
    DEFAULT_CAPTURE_PORT,
    FLOW_IDLE_SECONDS,
    HEALTH_BACKEND_DOWN,
    HEALTH_DECODING,
    HEALTH_HANDSHAKE_PENDING,
    HEALTH_MISSED_HANDSHAKE,
    HEALTH_NO_CONNECTION,
    CapturePositionClient,
    age_seconds,
    build_position,
    capture_port_candidates,
    capture_port_range_label,
    flow_liveness,
    flow_status_is_missed_handshake,
    flow_status_is_terminal,
    looks_like_capture_state,
    parse_health,
    position_key,
    probe_capture_state,
    resolve_map_id,
    url_port,
)
from nav_grid_editor.services.maps import MapService


def live_flow(flow_id=3, last_packet=None, status="正在解码"):
    return {"id": flow_id, "status": status, "lastPacket": last_packet}


def live_state(*, x=172.04, y=105.25, z=47.89, now=None, flow_id=3,
               status="正在解码", level_id="map02_lv009", map_id="map02"):
    now = time.time() if now is None else now
    return {
        "status": "运行中",
        "flows": [live_flow(flow_id, last_packet=now, status=status)],
        "lastPosition": {
            "x": x, "y": y, "z": z, "yaw": 308.8, "speed": 5.75,
            "time": now, "scene": 361, "levelId": level_id, "levelName": "雪松林",
            "mapId": map_id, "mapName": "武陵", "flow": flow_id,
        },
    }


class PortScanHelpersTest(unittest.TestCase):
    def test_candidates_go_up_from_8765_to_8775(self):
        ports = capture_port_candidates()
        self.assertEqual(DEFAULT_CAPTURE_PORT, 8765)
        self.assertEqual(CAPTURE_PORT_START, 8765)
        self.assertEqual(CAPTURE_PORT_END, 8775)
        self.assertEqual(ports[0], 8765)
        self.assertEqual(ports[-1], 8775)
        self.assertEqual(ports, list(range(8765, 8776)))

    def test_range_label(self):
        self.assertEqual(capture_port_range_label(), "8765–8775")

    def test_reversed_bounds_are_normalized(self):
        self.assertEqual(capture_port_candidates(8770, 8768), [8768, 8769, 8770])

    def test_looks_like_capture_state_rejects_foreign_json(self):
        self.assertTrue(looks_like_capture_state({"flows": []}))
        self.assertTrue(looks_like_capture_state({"lastPosition": None}))
        for payload in ([], None, "x", {"ok": True}, {"status": "运行中"}):
            with self.subTest(payload=payload):
                self.assertFalse(looks_like_capture_state(payload))

    def test_url_port(self):
        self.assertEqual(url_port("http://127.0.0.1:8764"), 8764)
        self.assertIsNone(url_port("http://127.0.0.1"))
        self.assertIsNone(url_port(""))

    def test_probe_rejects_a_port_that_is_open_but_not_the_backend(self):
        """本服务自己也监听 8765，会对 /api/state 回 404 —— 不能被误判成抓包后端。"""
        stub = _StateStub({"ok": True})  # 合法 JSON，但不是后端状态
        try:
            self.assertIsNone(probe_capture_state(stub.url, timeout=1.0))
        finally:
            stub.close()


class AgeSecondsTest(unittest.TestCase):
    """陈旧度必须用**墙钟**相减（曾用单调钟相减恒为负、被夹成 0，安全提示失效）。"""

    def test_wall_clock_difference(self):
        self.assertAlmostEqual(age_seconds({"time": 1000.0}, now=1003.5), 3.5)

    def test_future_timestamp_is_clamped_to_zero(self):
        self.assertEqual(age_seconds({"time": 1000.0}, now=999.0), 0.0)

    def test_missing_or_invalid_timestamp(self):
        for record in ({}, {"time": None}, {"time": "x"}, [], None, {"time": float("nan")}):
            with self.subTest(record=record):
                self.assertEqual(age_seconds(record, now=1000.0), 0.0)

    def test_defaults_to_real_wall_clock(self):
        age = age_seconds({"time": time.time() - 2.0})
        self.assertGreaterEqual(age, 1.9)
        self.assertLess(age, 5.0)


class FlowStatusTest(unittest.TestCase):
    def test_terminal_statuses(self):
        for status in ("连接已结束", "解码停止：解密后的消息号无效",
                       "缺少连接起点，请重新启动游戏"):
            with self.subTest(status=status):
                self.assertTrue(flow_status_is_terminal(status))

    def test_in_progress_statuses_are_not_terminal(self):
        for status in ("正在解码", "等待登录初始化", "等待 TCP 补包", "", None):
            with self.subTest(status=status):
                self.assertFalse(flow_status_is_terminal(status))

    def test_missed_handshake_only(self):
        self.assertTrue(flow_status_is_missed_handshake("缺少连接起点，请重新启动游戏"))
        self.assertFalse(flow_status_is_missed_handshake("连接已结束"))
        self.assertFalse(flow_status_is_missed_handshake("解码停止：解码后的消息号无效"))
        self.assertFalse(flow_status_is_missed_handshake("正在解码"))


class ParseHealthTest(unittest.TestCase):
    def test_decoding_wins(self):
        result = parse_health(live_state())
        self.assertEqual(result["health"], HEALTH_DECODING)
        self.assertEqual(result["flow_id"], 3)
        self.assertEqual(result["flow_status"], "正在解码")

    def test_handshake_pending(self):
        state = {"flows": [{"id": 1, "status": "等待登录初始化", "lastPacket": time.time()}]}
        self.assertEqual(parse_health(state)["health"], HEALTH_HANDSHAKE_PENDING)

    def test_missed_handshake(self):
        state = {"flows": [{"id": 1, "status": "缺少连接起点，请重新启动游戏"}]}
        self.assertEqual(parse_health(state)["health"], HEALTH_MISSED_HANDSHAKE)

    def test_only_dead_flows(self):
        state = {"flows": [{"id": 1, "status": "连接已结束"},
                           {"id": 2, "status": "解码停止：x"}]}
        self.assertEqual(parse_health(state)["health"], HEALTH_NO_CONNECTION)

    def test_idle_flow_is_skipped(self):
        state = {"flows": [{"id": 1, "status": "正在解码",
                            "lastPacket": time.time() - FLOW_IDLE_SECONDS - 5}]}
        self.assertEqual(parse_health(state)["health"], HEALTH_NO_CONNECTION)

    def test_live_flow_wins_over_old_missed_flow(self):
        """有条死连接 + 有条活连接要判成正常，否则每次正常抓包都被误判。"""
        state = {"flows": [
            {"id": 1, "status": "缺少连接起点，请重新启动游戏"},
            live_flow(2, last_packet=time.time()),
        ]}
        self.assertEqual(parse_health(state)["health"], HEALTH_DECODING)

    def test_missing_or_empty_flows(self):
        for state in ({}, {"flows": []}, {"flows": None}, [], None):
            with self.subTest(state=state):
                self.assertEqual(parse_health(state)["health"], HEALTH_NO_CONNECTION)


class FlowLivenessTest(unittest.TestCase):
    def test_missing_entry_is_not_alive(self):
        state = {"flows": [live_flow(1, last_packet=time.time())]}
        self.assertEqual(flow_liveness(state, {"flow": 9}, now=time.time())[2], False)

    def test_terminal_entry_is_not_alive(self):
        state = {"flows": [{"id": 1, "status": "连接已结束"}]}
        self.assertFalse(flow_liveness(state, {"flow": 1}, now=time.time())[2])

    def test_idle_entry_is_not_alive(self):
        now = time.time()
        state = {"flows": [live_flow(1, last_packet=now - FLOW_IDLE_SECONDS - 1)]}
        self.assertFalse(flow_liveness(state, {"flow": 1}, now=now)[2])

    def test_live_entry_is_alive(self):
        now = time.time()
        state = {"flows": [live_flow(1, last_packet=now - 1)]}
        flow_id, status, alive = flow_liveness(state, {"flow": 1}, now=now)
        self.assertEqual(flow_id, 1)
        self.assertEqual(status, "正在解码")
        self.assertTrue(alive)

    def test_no_evidence_counts_as_alive(self):
        """缺信息不等于会话已死：没有证据就不扣信任。"""
        for state, record in (({"flows": [{"id": 1, "status": "连接已结束"}]}, {"x": 1}),
                              ({}, {"flow": 1}),
                              ({"flows": "x"}, {"flow": 1})):
            with self.subTest(record=record):
                self.assertTrue(flow_liveness(state, record, now=time.time())[2])


class BuildPositionTest(unittest.TestCase):
    def test_fields_map_to_the_map_service_contract(self):
        state = live_state(x=1.5, y=2.5, z=-3.5)
        pos = build_position(state["lastPosition"], state)
        self.assertEqual(pos["pos"], {"x": 1.5, "y": 2.5, "z": -3.5})
        self.assertEqual(pos["mapId"], "map02")
        self.assertEqual(pos["levelId"], "map02_lv009")
        self.assertEqual(pos["scene"], 361)
        self.assertEqual(pos["levelName"], "雪松林")
        self.assertEqual(pos["mapName"], "武陵")
        self.assertEqual(pos["yaw"], 308.8)
        self.assertEqual(pos["speed"], 5.75)
        self.assertEqual(pos["flow"], 3)
        self.assertEqual(pos["source"], "capture")

    def test_incomplete_coordinates_are_rejected(self):
        for record in (None, [], {}, {"x": 1, "y": 2}, {"x": 1, "y": 2, "z": "x"},
                       {"x": 1, "y": 2, "z": float("inf")}):
            with self.subTest(record=record):
                self.assertIsNone(build_position(record))

    def test_scene_table_map_wins(self):
        self.assertEqual(resolve_map_id({"mapId": "map02", "levelId": "map02_lv009"}),
                         ("map02", "scene_table"))

    def test_known_scene_without_parent_map_does_not_guess(self):
        """试炼场这类场景有自己的坐标系，没有大地图就返回空串，不按坐标猜。"""
        state = {"ws": {"lastPosition": {"mapId": "map02"}}}
        self.assertEqual(resolve_map_id({"levelId": "blackbox_1", "mapId": ""}, state),
                         ("", "no_parent_map"))

    def test_unknown_scene_falls_back_to_map_ws(self):
        state = {"ws": {"lastPosition": {"mapId": "map03"}}}
        self.assertEqual(resolve_map_id({"levelId": "", "mapId": ""}, state),
                         ("map03", "backend_ws"))
        self.assertEqual(resolve_map_id({"levelId": "", "mapId": ""}, {}), ("", "unknown"))

    def test_position_key_dedupes_identical_samples(self):
        state = live_state()
        a = build_position(state["lastPosition"], state)
        b = build_position(dict(state["lastPosition"]), state)
        self.assertEqual(position_key(a), position_key(b))
        changed = dict(state["lastPosition"], time=state["lastPosition"]["time"] + 1)
        self.assertNotEqual(position_key(a), position_key(build_position(changed, state)))


class _StateStub:
    """最小 /api/state 服务，用来做不依赖真后端的端到端。"""

    def __init__(self, payload, port=0):
        self.payload = payload
        self.requests = 0
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != "/api/state":
                    self.send_error(404)
                    return
                stub.requests += 1
                body = json.dumps(stub.payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # 静音
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class CaptureClientStubTest(unittest.TestCase):
    def test_poll_maps_position_and_dedupes(self):
        stub = _StateStub(live_state())
        seen = []
        client = CapturePositionClient(on_position=seen.append, base_url=stub.url,
                                       poll_interval=0.05)
        try:
            client.start()
            self.assertTrue(_wait_for(lambda: seen), "没有收到位置")
            self.assertTrue(_wait_for(lambda: client.status()["connected"]))
            time.sleep(0.25)  # 同一份坐标轮询多次
            status = client.status()
        finally:
            client.stop()
            stub.close()
        self.assertEqual(len(seen), 1, "同一份坐标只应上报一次")
        self.assertEqual(status["source"], "capture")
        self.assertEqual(status["health"], HEALTH_DECODING)
        self.assertEqual(status["last_position"]["mapId"], "map02")
        self.assertGreaterEqual(status["age_seconds"], 0.0)
        self.assertGreater(stub.requests, 1, "应当持续轮询")

    def test_dead_flow_position_is_not_emitted(self):
        state = live_state()
        state["flows"] = [{"id": 3, "status": "连接已结束", "lastPacket": time.time()}]
        stub = _StateStub(state)
        seen = []
        client = CapturePositionClient(on_position=seen.append, base_url=stub.url,
                                       poll_interval=0.05)
        try:
            client.start()
            self.assertTrue(_wait_for(lambda: client.status()["last_position"] is not None))
            self.assertFalse(client.status()["position_alive"])
            self.assertIn("结束的连接", client.status()["hint"])
            time.sleep(0.15)
        finally:
            client.stop()
            stub.close()
        self.assertEqual(seen, [], "死连接上的旧坐标不能画到地图上")

    def test_backend_down_reports_hint(self):
        stub = _StateStub(live_state())
        url = stub.url
        stub.close()  # 端口已释放 → 连不上
        client = CapturePositionClient(on_position=lambda _d: None, base_url=url,
                                       poll_interval=0.05)
        try:
            client.start()
            # health 的初值就是 backend_down，所以要等**这一轮真的失败过**（last_error 落上）
            self.assertTrue(_wait_for(lambda: bool(client.status()["last_error"])))
            status = client.status()
            self.assertFalse(status["connected"])
            self.assertEqual(status["health"], HEALTH_BACKEND_DOWN)
            self.assertIn("连接不到抓包后端", status["hint"])
            self.assertTrue(status["last_error"])
        finally:
            client.stop()

    def test_decoding_without_position_is_not_connected(self):
        """刚握上手、还没解出坐标：flow_decoding 为真，但「已连接」要等坐标。"""
        state = live_state()
        state["lastPosition"] = None
        stub = _StateStub(state)
        client = CapturePositionClient(on_position=lambda _d: None, base_url=stub.url,
                                       poll_interval=0.05)
        try:
            client.start()
            self.assertTrue(_wait_for(lambda: client.status()["flow_decoding"]))
            self.assertTrue(_wait_for(
                lambda: "等待第一条坐标" in client.status()["hint"]))
            status = client.status()
        finally:
            client.stop()
            stub.close()
        self.assertFalse(status["connected"])
        self.assertIsNone(status["last_position"])


class PortScanTest(unittest.TestCase):
    """默认从 8765 升序扫到 8775，锁定第一个真正是抓包后端的。"""

    def test_scan_skips_dead_and_foreign_ports_and_reports_the_port(self):
        real = _StateStub(live_state())          # 真实后端，端口 R
        try:
            try:
                # 端口是开的、但不返回后端状态（对应本服务自己占着 8765 的情形）
                decoy = _StateStub({"ok": True}, port=real.port - 1)
            except OSError:
                self.skipTest("找不到相邻的两个空闲端口")
            try:
                seen = []
                client = CapturePositionClient(
                    on_position=seen.append, base_url=None,
                    port_start=real.port - 2, port_end=real.port,  # 候选: R-2, R-1, R
                    poll_interval=0.05, scan_cooldown=0.0)
                try:
                    client.start()
                    self.assertTrue(_wait_for(lambda: seen), "没有扫到真正的后端")
                    status = client.status()
                finally:
                    client.stop()
                self.assertEqual(status["capture_port"], real.port)
                self.assertEqual(status["backend_url"], real.url)
                self.assertEqual(status["port_mode"], "scanned")
                self.assertTrue(status["connected"])
            finally:
                decoy.close()
        finally:
            real.close()

    def test_fixed_base_url_disables_scanning(self):
        stub = _StateStub(live_state())
        try:
            client = CapturePositionClient(on_position=lambda _d: None,
                                           base_url=stub.url, poll_interval=0.05)
            try:
                client.start()
                self.assertTrue(_wait_for(lambda: client.status()["connected"]))
                status = client.status()
            finally:
                client.stop()
            self.assertEqual(status["port_mode"], "configured")
            self.assertEqual(status["capture_port"], stub.port)
        finally:
            stub.close()

    def test_nothing_listening_reports_the_scanned_range(self):
        probe = _StateStub(live_state())
        free_port = probe.port
        probe.close()  # 端口空出来，保证这一段的候选全都没人应答
        client = CapturePositionClient(on_position=lambda _d: None, base_url=None,
                                       port_start=free_port, port_end=free_port + 2,
                                       poll_interval=0.05, scan_cooldown=0.0)
        try:
            client.start()
            self.assertTrue(_wait_for(
                lambda: "没有找到抓包后端" in client.status()["hint"]))
            status = client.status()
        finally:
            client.stop()
        self.assertIsNone(status["capture_port"])
        self.assertEqual(status["backend_url"], "")
        self.assertIn(capture_port_range_label(free_port, free_port + 2), status["hint"])
        self.assertEqual(status["health"], HEALTH_BACKEND_DOWN)


class CaptureThroughMapServiceTest(unittest.TestCase):
    """轮询 → MapService → SSE coords 事件，整条链路走一遍。"""

    def test_position_reaches_the_sse_bus(self):
        stub = _StateStub(live_state(x=10.0, y=20.0, z=30.0))
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            svc = MapService(data_root=root, tiles_root=root / "t",
                             profile_dir=root / "p", grid2d_dir=root / "g",
                             capture_url=stub.url)
            queue = svc.bus.subscribe()
            try:
                svc.start_position_stream()
                evt = None
                deadline = time.time() + 5
                while time.time() < deadline:
                    try:
                        candidate = json.loads(queue.get(timeout=0.5))
                    except Exception:
                        continue
                    if candidate.get("type") == "coords":
                        evt = candidate
                        break
            finally:
                svc.stop_position_stream()
                svc.bus.unsubscribe(queue)
                stub.close()
        self.assertIsNotNone(evt, "SSE 上没有等到 coords 事件")
        self.assertEqual(evt["data"]["mapId"], "map02")
        self.assertEqual(evt["data"]["pos"], {"x": 10.0, "y": 20.0, "z": 30.0})
        # 楼层判定会补上 gridFloor* 字段（没有分层数据时是 main）
        self.assertIn("gridFloorStatus", evt["data"])


if __name__ == "__main__":
    unittest.main()
