# -*- coding: utf-8 -*-
"""HTTP 层（server.Handler）的回归测试。

重点是 POST 的统一异常边界：前端一律 `await resp.json()`，所以服务层漏出的
任何异常都必须变成一条 JSON 响应，而不是"连接被断开、前端只看到一句没有原因
的失败"。这里真的起一个 HTTP 服务来打，而不是只调函数。
"""

import json
import tempfile
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


if __name__ == "__main__":
    unittest.main()
