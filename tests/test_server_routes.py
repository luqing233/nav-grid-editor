# -*- coding: utf-8 -*-
"""HTTP 层（server.Handler）的回归测试。

重点是 POST 的统一异常边界：前端一律 `await resp.json()`，所以服务层漏出的
任何异常都必须变成一条 JSON 响应，而不是"连接被断开、前端只看到一句没有原因
的失败"。这里真的起一个 HTTP 服务来打，而不是只调函数。
"""

import json
import tempfile
import time
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from nav_grid_editor import server
from nav_grid_editor.map_service import MapService


class PostBoundaryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self.svc = MapService(data_root=d, tiles_root=d / "tiles",
                              profile_dir=d / "p", grid2d_dir=d / "grids")
        server.service = self.svc
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.t.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._tmp.cleanup()

    def _post(self, path, body):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=body.encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def test_non_object_body_gets_json_400_not_a_dropped_connection(self):
        """body 是合法 JSON 但不是对象（[] / 3 / "x" / null）时，
        以前 req.get() 抛 AttributeError，连接被断开、没有任何 JSON。
        """
        for path in ("/api/compose", "/api/grid2d", "/api/calib", "/api/fetch/start"):
            for body in ("[]", "3", '"x"', "null"):
                with self.subTest(path=path, body=body):
                    status, res = self._post(path, body)
                    self.assertEqual(status, 400)
                    self.assertFalse(res["ok"])
                    self.assertTrue(res["error"])

    def test_unparseable_body_gets_json_400(self):
        for path in ("/api/compose", "/api/grid2d", "/api/calib"):
            with self.subTest(path=path):
                status, res = self._post(path, "{not json")
                self.assertEqual(status, 400)
                self.assertFalse(res["ok"])

    def test_service_exception_becomes_json_500_not_a_dropped_connection(self):
        """do_POST 的统一异常边界：服务层漏出的异常也必须是 JSON。

        这条防的是"未知的畸形输入"——具体是哪个字段能触发不重要，重要的是
        任何异常都不会让前端拿到一个没有原因的失败。
        """
        boom = ValueError("模拟服务层异常")
        with mock.patch.object(MapService, "save_grid2d", side_effect=boom):
            status, res = self._post("/api/grid2d", {"map": "m", "zoom": "4", "data": {}})
        self.assertEqual(status, 500)
        self.assertFalse(res["ok"])
        self.assertIn("模拟服务层异常", res["error"])

    def test_normal_request_still_works(self):
        """边界不能把正常请求也变成错误。"""
        status, res = self._post("/api/grid2d", {
            "map": "m", "zoom": "4",
            "data": {"origin": [0, 0, 0], "cell_size": 1.0,
                     "cells": [[1, 1]], "blocked": []}})
        self.assertEqual(status, 200)
        self.assertTrue(res["ok"], res)


class ConditionalGetTest(unittest.TestCase):
    """条件请求（ETag / 304）。

    这是"浏览地图时不反复重下"的关键：瓦片以前是 max-age=30 且没有 ETag，
    等于每 30 秒把视野里那几百张全部重下一遍。现在带 ETag，过期后重验只回
    304（不重传正文）。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self.tiles = d / "tiles"
        (self.tiles / "latest" / "m" / "4").mkdir(parents=True)
        (self.tiles / "latest" / "m" / "4" / "0_0.png").write_bytes(b"tile-bytes")
        (self.tiles / "maps" / "m" / "4").mkdir(parents=True)
        (self.tiles / "maps" / "m" / "4" / "m_4.png").write_bytes(b"composite")
        self.svc = MapService(data_root=d, tiles_root=self.tiles,
                              profile_dir=d / "p", grid2d_dir=d / "grids")
        server.service = self.svc
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._tmp.cleanup()

    def _get(self, path, extra=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     headers=extra or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def _check_revalidation(self, path):
        st, body, hdr = self._get(path)
        self.assertEqual(st, 200, path)
        self.assertTrue(body)
        etag = hdr.get("ETag")
        self.assertTrue(etag, f"{path} 没有 ETag")
        self.assertIn("max-age", hdr.get("Cache-Control", ""))
        st2, body2, _ = self._get(path, {"If-None-Match": etag})
        self.assertEqual(st2, 304, f"{path} 命中了 ETag 却回 {st2}")
        self.assertEqual(body2, b"", "304 不能带正文")

    def test_tile_revalidates_with_etag(self):
        self._check_revalidation("/tiles/m/4/0_0.png")

    def test_composite_revalidates_with_etag(self):
        self._check_revalidation("/maps/m/4/m_4.png")

    def test_etag_changes_when_content_changes(self):
        """ETag 必须跟着内容走：瓦片重抓后内容变了，不能还回 304。"""
        _, _, h1 = self._get("/tiles/m/4/0_0.png")
        p = self.tiles / "latest" / "m" / "4" / "0_0.png"
        time.sleep(0.01)
        p.write_bytes(b"different-bytes")
        st, body, h2 = self._get("/tiles/m/4/0_0.png", {"If-None-Match": h1["ETag"]})
        self.assertEqual(st, 200, "内容变了却回了 304")
        self.assertEqual(body, b"different-bytes")


if __name__ == "__main__":
    unittest.main()
