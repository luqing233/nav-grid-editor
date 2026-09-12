# -*- coding: utf-8 -*-
"""瓦片目录合并的回归测试。

抓取会话目录（run_*）只存本次**新增/变化**的瓦片：没变化的由本地缓存直接
应答、不写盘。所以合成/边界统计必须读"所有目录的并集"，不能只读第一个
存在的目录——历史上"合成总图只有一小块、不含以前抓的"就是这个 bug。
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nav_grid_editor import map_service
from nav_grid_editor.map_service import MapService, TileStore


def _tile(root: Path, rel_dir: str, x: int, y: int, tag: bytes) -> Path:
    p = root / rel_dir / f"{x}_{y}.png"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(tag)
    return p


class TileMergeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = TileStore(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_merges_latest_and_active_session(self):
        """抓取中：latest 的老瓦片 + 会话里的新瓦片都要出现，同坐标以会话为准。"""
        _tile(self.root, "latest/m/4", 0, 0, b"old-0-0")
        _tile(self.root, "latest/m/4", 1, 0, b"old-1-0")
        _tile(self.root, "run_20260101_000000/m/4", 1, 0, b"new-1-0")
        _tile(self.root, "run_20260101_000000/m/4", 2, 0, b"new-2-0")
        self.store.active_session_dir = self.root / "run_20260101_000000"

        got = {(x, y): f.read_bytes() for x, y, f in self.store.iter_tiles("m", "4")}
        self.assertEqual(sorted(got), [(0, 0), (1, 0), (2, 0)])
        self.assertEqual(got[(0, 0)], b"old-0-0")   # 老瓦片没被丢掉
        self.assertEqual(got[(1, 0)], b"new-1-0")   # 同坐标新的覆盖旧的

    def test_resolve_dir_alone_would_miss_tiles(self):
        """反例固定：resolve_dir 只返回一个目录（保留它做存在性判断用）。"""
        _tile(self.root, "latest/m/4", 0, 0, b"old")
        _tile(self.root, "run_20260101_000000/m/4", 5, 5, b"new")
        self.store.active_session_dir = self.root / "run_20260101_000000"

        one_dir = list((self.store.resolve_dir("m", "4")).iterdir())
        merged = list(self.store.iter_tiles("m", "4"))
        self.assertEqual(len(one_dir), 1)     # 只看单个目录 → 只有 1 张
        self.assertEqual(len(merged), 2)      # 合并后 → 2 张

    def test_newer_session_overrides_older(self):
        _tile(self.root, "run_20260101_000000/m/4", 1, 1, b"older")
        _tile(self.root, "run_20260202_000000/m/4", 1, 1, b"newer")
        got = {(x, y): f.read_bytes() for x, y, f in self.store.iter_tiles("m", "4")}
        self.assertEqual(got[(1, 1)], b"newer")

    def test_saved_map_info_uses_merged_bounds(self):
        """已保存总图的边界要按合并后的全集算，否则前端贴图会错位。"""
        tiles_root = self.root / "tiles"
        svc = MapService(data_root=self.root, tiles_root=tiles_root,
                         profile_dir=self.root / "profile", grid2d_dir=self.root / "grids")
        _tile(tiles_root, "latest/m/4", -2, -3, b"a")
        _tile(tiles_root, "latest/m/4", 4, 5, b"b")
        _tile(tiles_root, "run_20260101_000000/m/4", 7, 8, b"c")
        svc.store.active_session_dir = tiles_root / "run_20260101_000000"
        png = tiles_root / "maps" / "m" / "4" / "m_4.png"
        png.parent.mkdir(parents=True, exist_ok=True)
        png.write_bytes(b"png")

        info = svc.saved_map_info("m", "4")
        self.assertEqual(
            (info["minX"], info["maxX"], info["minY"], info["maxY"], info["count"]),
            (-2, 7, -3, 8, 3),
        )


class ComposeRunRobustnessTest(unittest.TestCase):
    """_compose_run 的异常路径：出错也必须把 compose_done 发出去。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.svc = MapService(
            data_root=self.root, tiles_root=self.root / "tiles",
            profile_dir=self.root / "profile", grid2d_dir=self.root / "grids")
        self.svc._compose = {"map": "m", "zoom": "4", "total": 1, "done": 0,
                             "running": True}

    def tearDown(self):
        self._tmp.cleanup()

    def test_canvas_failure_still_emits_compose_done(self):
        """画布创建失败时不能在 finally 里因 file_rel 未绑定而抛 NameError。

        否则 compose_done 发不出去，前端永远停在"合成中"，而线程里冒出来的
        是 NameError，把真正的失败原因盖掉。
        """
        p = self.root / "tiles" / "latest" / "m" / "4" / "0_0.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")

        class BoomImage:  # 真 Image 存在（truthy），但建画布就炸
            @staticmethod
            def new(*a, **kw):
                raise RuntimeError("画布创建失败")

        q = self.svc.bus.subscribe()
        try:
            with mock.patch.object(map_service, "Image", BoomImage):
                self.svc._compose_run("m", "4", [(0, 0, p)], save=True)
        finally:
            events = []
            while not q.empty():
                events.append(json.loads(q.get_nowait()))
            self.svc.bus.unsubscribe(q)

        done = [e for e in events if e.get("type") == "compose_done"]
        self.assertEqual(len(done), 1, f"compose_done 没发出来，事件: {events}")
        self.assertIsNone(done[0]["file"])
        self.assertFalse(self.svc._compose["running"])


if __name__ == "__main__":
    unittest.main()
