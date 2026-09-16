# -*- coding: utf-8 -*-
"""地图标定（MapService.save_calib）的回归测试。

保护两条：
- **畸形请求体只能变成 JSON 错误**，不能抛异常冒到 do_POST。do_POST 虽然有
  统一的异常边界兜底，但边界是最后一道防线；服务层自己该拒的要自己拒，
  否则错误信息就只剩一句笼统的"服务端异常"。
- **写进 mapping.json 的数字必须是有限的**：json.loads 接受 NaN/Infinity 这类
  非标准字面量，一旦写进去，之后每次像素↔世界换算都会静默变成 nan。
"""

import json
import math
import tempfile
import unittest
from pathlib import Path

from nav_grid_editor.services.maps import MapService


def _pts(n=3):
    """n 个分散且不共线的控制点。"""
    base = [[0, 0, 0, 0], [100, 0, 10, 0], [0, 100, 0, 10], [100, 100, 10, 10]]
    return [{"pixel": [p[0], p[1]], "world": [p[2], p[3]]} for p in base[:n]]


class SaveCalibValidationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self.svc = MapService(tiles_root=d / "tiles", profile_dir=d / "p",
                              grid2d_dir=d / "grids", data_root=d)

    def tearDown(self):
        self._tmp.cleanup()

    def _save(self, points=None, image_size=None, **kw):
        return self.svc.save_calib("m", "4", points if points is not None else _pts(),
                                   image_size if image_size is not None else {}, **kw)

    # ---------- 畸形请求体 ----------

    def test_malformed_points_return_error_not_exception(self):
        for pts in ([1, 2, 3],
                    [{"pixel": [1], "world": [1, 2]}, *_pts()[:2]],
                    [{"pixel": "ab", "world": [1, 2]}, *_pts()[:2]],
                    [{"pixel": [1, 2]}, *_pts()[:2]]):
            with self.subTest(points=str(pts)[:40]):
                res = self._save(points=pts)
                self.assertFalse(res["ok"])
                self.assertTrue(res.get("error"))

    def test_non_numeric_image_size_is_coerced_not_raised(self):
        """image_size 的值直接来自 JSON：int("abc") 会抛，NaN/Infinity 也会。

        这些以前会冒出 save_calib、冒到 do_POST，最后是一条没有 JSON 的断连。
        转不动的当 0，能当数字看的（"12.5"）按 12 处理——重点是**不抛异常**。
        """
        for w, expected in (("abc", 0), ({"a": 1}, 0), ([1], 0),
                            (None, 0), ("12.5", 12), (7, 7)):
            with self.subTest(width=w):
                res = self._save(image_size={"width": w, "height": 10})
                self.assertTrue(res["ok"], res)
                self.assertEqual(res["image_size"]["width"], expected)

    def test_non_finite_image_size_is_coerced(self):
        for w in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(width=w):
                res = self._save(image_size={"width": w, "height": 10})
                self.assertTrue(res["ok"], res)
                self.assertEqual(res["image_size"]["width"], 0)

    def test_image_size_that_is_not_a_dict_is_tolerated(self):
        res = self._save(image_size=5)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["image_size"], {"width": 0, "height": 0})

    def test_degenerate_affine_returns_error_not_exception(self):
        """控制点像素互不相同但世界坐标全一样 -> 仿射退化，求逆抛 ValueError。

        这个异常以前漏出 save_calib，前端只看到"标定请求失败"，没有任何原因。
        """
        same_world = [{"pixel": [0, 0], "world": [0, 0]},
                      {"pixel": [10, 0], "world": [0, 0]},
                      {"pixel": [0, 10], "world": [0, 0]}]
        res = self._save(points=same_world)
        self.assertFalse(res["ok"])
        self.assertIn("拟合失败", res["error"])

    # ---------- 有限数 ----------

    def test_non_finite_control_points_are_rejected(self):
        """NaN 控制点会算出一个 NaN 矩阵，json.loads 又照单全收——写进去就成了
        一张"看着已标定、换算全变 nan"的地图。"""
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(world_x=bad):
                pts = _pts()
                pts[0]["world"][0] = bad
                res = self._save(points=pts)
                self.assertFalse(res["ok"])
                self.assertIn("有限数", res["error"])
                self.assertFalse(self.svc.calib_file("m", "4"),
                                 "被拒的标定不该留下文件")

    def test_finite_but_huge_control_points_are_rejected(self):
        """有限但极大的坐标会在 RANSAC 的距离/误差平方里溢出成 OverflowError。

        那个异常是从拟合内部抛的，比 ValueError 更隐蔽；以前会一路冒出
        save_calib，前端只看到"服务端异常"。
        """
        for w in (1e13, 1e200, -1e200):
            with self.subTest(world=w):
                pts = [{"pixel": [10, 20], "world": [w, 0]},
                       {"pixel": [300, 30], "world": [0, w]},
                       {"pixel": [50, 400], "world": [w, w]}]
                res = self._save(points=pts)
                self.assertFalse(res["ok"])
                self.assertIn("范围", res["error"])

    def test_normal_magnitudes_still_accepted(self):
        """量级上限不能误伤正常坐标（游戏世界坐标在千级，像素在万级）。"""
        pts = [{"pixel": [0, 0], "world": [-5000, -5000]},
               {"pixel": [4096, 0], "world": [5000, -5000]},
               {"pixel": [0, 4096], "world": [-5000, 5000]}]
        res = self._save(points=pts)
        self.assertTrue(res["ok"], res)

    def test_saved_mapping_is_finite_and_reloadable(self):
        res = self._save(image_size={"width": 1920, "height": 1080})
        self.assertTrue(res["ok"], res)
        p = self.svc.calib_file("m", "4")
        text = p.read_text(encoding="utf-8")
        self.assertNotIn("NaN", text)
        self.assertNotIn("Infinity", text)
        for v in json.loads(text)["matrix"][0] + json.loads(text)["matrix"][1]:
            self.assertTrue(math.isfinite(v))

    # ---------- enabled / inlier 分离 ----------

    def test_enabled_keeps_user_choice_and_inlier_is_separate(self):
        """enabled 以前被写成 RANSAC 的内点标记，重新加载后复选框会变成上一次的
        拟合结论，用户的选择被静默改写。"""
        pts = _pts(4)
        pts[3]["world"] = [999, 999]      # 明显离群，但用户没取消勾选
        res = self._save(points=pts)
        self.assertTrue(res["ok"], res)
        cps = res["control_points"]
        self.assertEqual(len(cps), 4)
        self.assertTrue(all(cp["enabled"] is True for cp in cps),
                        "用户勾选的 enabled 被改写了")
        self.assertEqual(cps[3]["inlier"], False, "离群点应被标成非内点")
        self.assertTrue(all(cp["inlier"] is True for cp in cps[:3]))

    def test_disabled_points_are_persisted_as_disabled(self):
        pts = _pts(4)
        pts[3]["enabled"] = False
        res = self._save(points=pts)
        self.assertTrue(res["ok"], res)
        self.assertFalse(res["control_points"][3]["enabled"])
        self.assertIsNone(res["control_points"][3]["inlier"],
                          "未参与拟合的点没有内点结论")


class CalibNameValidationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self.svc = MapService(tiles_root=d / "tiles", profile_dir=d / "p",
                              grid2d_dir=d / "grids", data_root=d)

    def tearDown(self):
        self._tmp.cleanup()

    def test_reserved_device_name_is_rejected(self):
        """NUL 之类的名字能过字符白名单，但 mkdir 会抛 WinError 3。"""
        for name in ("NUL", "CON", "com1", "LPT9"):
            with self.subTest(map_name=name):
                res = self.svc.save_calib(name, "4", _pts(), {})
                self.assertFalse(res["ok"])
                self.assertIn("非法", res["error"])


if __name__ == "__main__":
    unittest.main()
