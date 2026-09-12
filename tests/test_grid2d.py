# -*- coding: utf-8 -*-
"""2D 网格读写（map_service.get_grid2d / save_grid2d）的回归测试。

运行：
    .venv\\Scripts\\python.exe -m unittest discover -s tests -t .

重点保护"读进来再存回去不能静默丢数据"这条不变量：编辑器只看得到已涂格子，
如果保存范围只按已涂格子重建，带未知边框的网格会被整圈裁掉。
"""

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from nav_grid_editor.map_service import (
    GRID_AXIS_CONVENTION,
    GRID_MAGIC,
    GRID_SCHEMA_VERSION,
    MapService,
)


def _meta(**kw) -> str:
    base = {
        "magic": GRID_MAGIC,
        "schema_version": GRID_SCHEMA_VERSION,
        "map_name": "m",
        "zoom": "4",
        "origin": [10.0, 0.0, 20.0],
        "cell_size": 1.0,
        "axis_convention": GRID_AXIS_CONVENTION,
    }
    base.update(kw)
    return json.dumps(base, ensure_ascii=False)


class Grid2DRoundTripTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self.svc = MapService(grid2d_dir=d, tiles_root=d / "tiles")

    def tearDown(self):
        self._tmp.cleanup()

    def _save(self, **data):
        payload = {"origin": [10, 0, 20], "cell_size": 1, "cells": [], "blocked": []}
        payload.update(data)
        return self.svc.save_grid2d("m", "4", payload)

    def test_new_grid_falls_back_to_touched_bbox(self):
        """没有 shape（新建网格）时保持旧行为：边界盒决定范围，origin 跟着平移。"""
        res = self._save(cells=[[1, 1]], blocked=[[2, 2]])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["shape"], [2, 2])
        self.assertEqual(res["shift"], [1, 1])
        self.assertEqual(res["origin"][0], 11.0)
        self.assertEqual(res["origin"][2], 21.0)

    def test_shape_keeps_unknown_margin(self):
        """带 shape 保存时未知边框必须原样保留，origin 不漂移。"""
        res = self._save(shape=[5, 5], cells=[[1, 1]], blocked=[[2, 2]])
        self.assertEqual(res["shape"], [5, 5])
        self.assertEqual(res["shift"], [0, 0])
        data = self.svc.get_grid2d("m", "4")["data"]
        self.assertEqual(data["shape"], [5, 5])
        self.assertEqual([data["origin"][0], data["origin"][2]], [10.0, 20.0])
        self.assertEqual(data["cells"], [[1, 1]])
        self.assertEqual(data["blocked"], [[2, 2]])

    def test_painting_outside_expands_range_and_reports_shift(self):
        """涂到原范围外只扩不缩，并回报下标基准要回移多少。"""
        res = self._save(shape=[5, 5], cells=[[-2, 1]], blocked=[[2, 2]])
        self.assertEqual(res["shape"], [5, 7])
        self.assertEqual(res["shift"], [-2, 0])
        self.assertEqual(res["origin"][0], 8.0)
        data = self.svc.get_grid2d("m", "4")["data"]
        self.assertEqual(data["cells"], [[0, 1]])
        self.assertEqual(data["blocked"], [[4, 2]])

    def test_all_unknown_grid_is_saveable_with_shape(self):
        """全未知网格是合法状态（用于扩范围）；但没有范围信息时必须拒绝。"""
        self.assertTrue(self._save(shape=[3, 4])["ok"])
        res = self._save()
        self.assertFalse(res["ok"])
        self.assertIn("无法确定写多大", res["error"])

    def test_saved_file_meets_reader_spec(self):
        """落盘文件必须能被同级校验读回：成员恰好两个、uint8、值域 0/1/2。"""
        self._save(shape=[3, 3], cells=[[0, 0]], blocked=[[1, 1]])
        with np.load(self.svc.grid2d_path("m", "4"), allow_pickle=False) as z:
            self.assertEqual(sorted(z.files), ["cells", "meta"])
            self.assertEqual(z["cells"].dtype, np.uint8)
            self.assertTrue(set(np.unique(z["cells"]).tolist()) <= {0, 1, 2})


class Grid2DReadValidationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)
        self.svc = MapService(grid2d_dir=self.d, tiles_root=self.d / "tiles")

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name, cells, meta=None, **extra):
        np.savez_compressed(self.d / name, cells=cells,
                            meta=np.array(meta if meta is not None else _meta()), **extra)

    def test_rejects_out_of_range_state(self):
        self._write("bad_4.grid.npz", np.array([[1, 7]], dtype=np.uint8))
        self.assertIn("非法取值", self.svc.get_grid2d("bad", "4")["error"])

    def test_rejects_non_uint8_dtype(self):
        self._write("bad_4.grid.npz", np.array([[1, 0]], dtype=np.int64))
        self.assertIn("uint8", self.svc.get_grid2d("bad", "4")["error"])

    def test_rejects_extra_npz_member(self):
        self._write("bad_4.grid.npz", np.array([[1, 0]], dtype=np.uint8),
                    extra=np.array([1]))
        self.assertIn("npz 成员", self.svc.get_grid2d("bad", "4")["error"])

    def test_rejects_wrong_magic(self):
        self._write("bad_4.grid.npz", np.array([[1, 0]], dtype=np.uint8),
                    meta=_meta(magic="something-else"))
        self.assertIn("magic", self.svc.get_grid2d("bad", "4")["error"])

    def test_axis_convention_mismatch_is_a_warning(self):
        self._write("bad_4.grid.npz", np.array([[1, 0]], dtype=np.uint8),
                    meta=_meta(axis_convention="别的约定"))
        res = self.svc.get_grid2d("bad", "4")
        self.assertIsNotNone(res["data"])
        self.assertTrue(any("axis_convention" in w for w in res["warnings"]))

    def test_read_rejects_path_traversal(self):
        """读路径也必须校验地图名：grid2d_path 是拼字符串，`../x` 能读到目录外。

        写路径（save_grid2d）一直有这道校验，读路径漏了就成了「读任意同格式
        npz」的路径穿越。这里用 grids2d/../ 下的合法 npz 固定住这个边界。
        """
        np.savez_compressed(
            self.d / "secret_4.grid.npz",
            cells=np.array([[1, 1]], dtype=np.uint8), meta=np.array(_meta()))
        for bad in ("../secret", "..\\secret", "a/b", "..", ""):
            with self.subTest(map_name=bad):
                res = self.svc.get_grid2d(bad, "4")
                self.assertIsNone(res["data"], f"{bad!r} 竟然读到了数据")
                self.assertIn("非法", res.get("error", ""))

    def test_read_accepts_zoom_only_digits(self):
        """zoom 同样不许带路径分隔符。"""
        res = self.svc.get_grid2d("m", "../4")
        self.assertIsNone(res["data"])
        self.assertIn("非法", res.get("error", ""))


if __name__ == "__main__":
    unittest.main()
