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

from nav_grid_editor.services import maps as map_service
from nav_grid_editor.services.maps import service as map_service_impl
from nav_grid_editor.services.maps import (
    FetchState,
    MapService,
    TileStore,
    tile_pattern,
    valid_map_name,
    valid_map_zoom,
)

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None


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
            with mock.patch.object(map_service_impl, "Image", BoomImage):
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


@unittest.skipIf(Image is None, "需要 Pillow")
class ComposeSaveSafetyTest(unittest.TestCase):
    """合成保存的边界：失败时绝不能覆盖已经合成好的总图。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.svc = MapService(
            data_root=self.root, tiles_root=self.root / "tiles",
            profile_dir=self.root / "profile", grid2d_dir=self.root / "grids")
        self.td = self.root / "tiles" / "latest" / "m" / "4"
        self.td.mkdir(parents=True, exist_ok=True)
        self.out = self.root / "tiles" / "maps" / "m" / "4" / "m_4.png"
        self.out.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _tile(self, x, y, color):
        Image.new("RGB", (256, 256), color).save(self.td / f"{x}_{y}.png")

    def _compose(self):
        tiles = list(self.svc.store.iter_tiles("m", "4"))
        self.svc._compose = {"map": "m", "zoom": "4", "total": len(tiles),
                             "done": 0, "running": True}
        self.svc._compose_run("m", "4", tiles, save=True)

    def test_successful_compose_pastes_tiles_into_bounds(self):
        """正常路径的贴图几何：两张瓦片要落在各自的格子里（左红右绿）。"""
        self._tile(0, 0, (200, 30, 30))
        self._tile(1, 0, (30, 200, 30))
        self._compose()
        with Image.open(self.out) as im:
            self.assertEqual(im.size, (512, 256))
            self.assertEqual(im.getpixel((5, 5)), (200, 30, 30))
            self.assertEqual(im.getpixel((261, 5)), (30, 200, 30))
        self.assertFalse(list(self.out.parent.glob("*.tmp")), "临时文件没清干净")

    def test_first_tile_failure_does_not_write_1x1_image(self):
        """第一张瓦片读取失败时，绝不能把画布停在 1x1 黑图再保存出去。

        画布以前只在 `if i == 1` 分支里按边界重建：第一张失败就永远走不到那个
        分支，后续瓦片 paste 到 1x1 上被静默裁掉，最后 canvas.save() 拿一张
        1x1 黑图覆盖掉已经合成好的总图。这里固定住"尺寸不能退化成 1x1"。

        注意：此时其余瓦片仍会被贴上去并覆盖旧总图——那是合成本来的语义
        （和"第 N 张失败"一致），不是这个 bug。真正"原样保留"的场景见下面
        全部瓦片都读不到的那个用例。
        """
        self._tile(0, 0, (200, 30, 30))
        self._tile(1, 0, (30, 200, 30))
        Image.new("RGB", (512, 256), (255, 255, 0)).save(self.out)

        tiles = list(self.svc.store.iter_tiles("m", "4"))
        tiles[0][2].unlink()          # 排序后的第一张 -> 读取失败
        self.svc._compose = {"map": "m", "zoom": "4", "total": len(tiles),
                             "done": 0, "running": True}
        self.svc._compose_run("m", "4", tiles, save=True)

        with Image.open(self.out) as im:
            self.assertEqual(im.size, (512, 256), "画布退化成了 1x1")
        self.assertFalse(list(self.out.parent.glob("*.tmp")), "临时文件没清干净")

    def test_compose_also_writes_the_overview(self):
        """合成总图时要顺手把缩略图也生成掉。

        这是"打开地图快"的关键：合成时整张 canvas 已经在内存里，缩略图只是多花
        零点几秒编码；否则首次打开要去解码那张几十 MB 的 PNG（map02@4 实测 4.6 秒）。
        """
        self._tile(0, 0, (200, 30, 30))
        self._tile(1, 0, (30, 200, 30))
        self._compose()
        ov = self.svc.overview_path("m", "4")
        self.assertIsNotNone(ov)
        self.assertTrue(ov.is_file(), "合成之后缩略图没生成")
        self.assertEqual(ov.suffix, ".webp" if map_service.WEBP_OK else ".png")
        with Image.open(ov) as im:
            self.assertEqual(im.size, (512, 256))

    def test_overview_is_much_smaller_than_the_composite(self):
        """缩略图要真的"缩"——WebP 下同一张图比 PNG 小一个数量级。"""
        for x in range(4):
            self._tile(x, 0, (x * 60, 120, 200 - x * 40))
        self._compose()
        src = self.out.stat().st_size
        ov = self.svc.overview_path("m", "4").stat().st_size
        self.assertLess(ov, src, "缩略图不比总图小")
        self.assertLessEqual(len(list(self.out.parent.glob("*.overview.*"))), 1,
                             "同时存在多份缩略图")

    def test_all_tiles_failing_leaves_existing_composite_untouched(self):
        """一张都没贴成功时不能保存：宁可留着旧总图，也不要写一张空图覆盖它。

        这是"已有总图不被破坏"这条不变量的真正的用例——以前没人覆盖过
        `pasted == 0` 这个分支。
        """
        self._tile(0, 0, (200, 30, 30))
        self._tile(1, 0, (30, 200, 30))
        Image.new("RGB", (512, 256), (255, 255, 0)).save(self.out)
        sentinel = self.out.read_bytes()

        tiles = list(self.svc.store.iter_tiles("m", "4"))
        for _x, _y, p in tiles:
            p.unlink()                # 全部读不到 -> pasted == 0
        self.svc._compose = {"map": "m", "zoom": "4", "total": len(tiles),
                             "done": 0, "running": True}
        self.svc._compose_run("m", "4", tiles, save=True)

        self.assertEqual(self.out.read_bytes(), sentinel, "旧总图被覆盖了")
        self.assertFalse(list(self.out.parent.glob("*.tmp")), "临时文件没清干净")


class TileNameValidationTest(unittest.TestCase):
    """瓦片 URL 里的地图名会拼进文件系统路径，必须在源头挡住。"""

    def test_tile_url_rejects_map_name_with_drive_letter(self):
        """group 1 以前是 [^/]+，能匹配 `C:`；而 session_dir / "C:/4/0_0.png"
        在 Windows 上会被 pathlib 当成绝对路径、丢掉基目录，等于把写入点
        交给远端页面摆布。"""
        self.assertIsNone(tile_pattern.search("https://x/tile/C:/4/0_0.png"))
        self.assertIsNotNone(tile_pattern.search("https://x/tile/map01/4/0_0.png"))

    def test_rejects_traversal_and_windows_device_names(self):
        for bad in ("../x", "a/b", "", "C:", "NUL", "con", "COM1", "LPT9"):
            with self.subTest(map_name=bad):
                self.assertFalse(valid_map_name(bad))
        for good in ("map01", "base01", "indie_dg007", "map-2", "COM0"):
            with self.subTest(map_name=good):
                self.assertTrue(valid_map_name(good))

    def test_zoom_must_be_digits(self):
        self.assertTrue(valid_map_zoom("map01", "4"))
        for bad in ("../4", "", "4x", "4/5"):
            with self.subTest(zoom=bad):
                self.assertFalse(valid_map_zoom("map01", bad))

    def test_tile_store_ignores_invalid_names(self):
        """TileStore 兜底：非法名一律当作"没有瓦片"，不漏给文件系统。"""
        tmp = tempfile.TemporaryDirectory()
        try:
            store = TileStore(Path(tmp.name))
            self.assertEqual(store._compose_candidates("../x", "4"), [])
            self.assertIsNone(store.tile_bytes("C:", "4", 0, 0))
            self.assertEqual(list(store.iter_tiles("NUL", "4")), [])
        finally:
            tmp.cleanup()


class FetchManifestTest(unittest.TestCase):
    """抓取清单：错误必须留下来，而且同一张瓦片只能有一个最终结论。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.state = FetchState(self.root / "run_x", None)

    def tearDown(self):
        self._tmp.cleanup()

    def _manifest(self):
        return json.loads((self.root / "run_x" / "manifest.json").read_text(encoding="utf-8"))

    def test_errors_are_persisted_even_with_no_new_or_updated_tiles(self):
        """全盘写失败时 new/updated 都是 0，以前会直接 return，刚记下来的
        "哪张瓦片为什么失败"全丢掉——恰恰是最需要这份记录的时候。"""
        self.state.counts["error"] += 1
        self.state.add_error("m/4/0_0.png", "写入失败 m/4/0_0.png: 磁盘满")
        self.state.save_manifest()
        self.assertEqual(self._manifest()["errors"],
                         ["写入失败 m/4/0_0.png: 磁盘满"])

    def test_nothing_at_all_still_writes_no_manifest(self):
        """没成功、也没出错（比如全是 unchanged）就不该产生清单文件。"""
        self.state.counts["unchanged"] += 5
        self.state.save_manifest()
        self.assertFalse((self.root / "run_x" / "manifest.json").exists())

    def test_successful_retry_supersedes_the_earlier_error(self):
        """先写失败、重试成功后，同一张瓦片不能既是 errors 又是 new。"""
        self.state.counts["error"] += 1
        self.state.add_error("m/4/0_0.png", "写入失败 m/4/0_0.png: 被占用")
        self.state.clear_error("m/4/0_0.png")
        self.state.manifest["new"].append("m/4/0_0.png")
        self.state.counts["new"] += 1
        self.state.save_manifest()
        m = self._manifest()
        self.assertEqual(m["errors"], [])
        self.assertEqual(m["new"], ["m/4/0_0.png"])


if __name__ == "__main__":
    unittest.main()
