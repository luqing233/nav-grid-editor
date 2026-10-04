# -*- coding: utf-8 -*-
"""HTTP 层（FastAPI 应用）的回归测试。

重点两条：
- **POST 的统一异常边界**：前端一律 `await resp.json()`，所以服务层漏出的任何
  异常都必须变成一条 JSON 响应，而不是"连接被断开、前端只看到一句没有原因的失败"。
- **条件请求（ETag / 304）**：瓦片以前是 max-age=30 且没有 ETag，等于每 30 秒把
  视野里那几百张全部重下一遍。

这里真的起一个 uvicorn 实例来打（而不是只调函数）——HTTP 语义（状态码、头、
304 不带正文）本身就是被测对象。
"""

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import uvicorn

from nav_grid_editor.api import app as server
from nav_grid_editor.services.maps import MapService


class _ServerMixin:
    """起一个真实 uvicorn 实例，端口交给系统分配。"""

    def _start_server(self, svc: MapService) -> None:
        server.service = svc
        config = uvicorn.Config(server.app, host="127.0.0.1", port=0,
                                log_level="warning", access_log=False)
        self.uv = uvicorn.Server(config)
        threading.Thread(target=self.uv.run, daemon=True).start()
        deadline = time.time() + 15
        while not self.uv.started:
            if time.time() > deadline:
                raise RuntimeError("uvicorn 启动超时")
            time.sleep(0.02)
        self.port = self.uv.servers[0].sockets[0].getsockname()[1]

    def _stop_server(self) -> None:
        self.uv.should_exit = True
        for _ in range(200):
            if not self.uv.started:
                break
            time.sleep(0.02)

    def _request(self, path, data=None, headers=None):
        """返回 (状态码, 正文, 响应头)。

        响应头**不要**转成 dict：uvicorn 会把头名小写化（`etag` 而不是 `ETag`），
        而 HTTPMessage 的 get 是大小写不敏感的，转成 dict 就把这个能力丢了。
        """
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            headers=headers or {},
            method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def _post_json(self, path, body):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        status, raw, _ = self._request(path, data=body.encode("utf-8"),
                                       headers={"Content-Type": "application/json"})
        return status, json.loads(raw.decode("utf-8"))


class PostBoundaryTest(_ServerMixin, unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self.svc = MapService(data_root=d, tiles_root=d / "tiles",
                              profile_dir=d / "p", grid2d_dir=d / "grids")
        self._start_server(self.svc)

    def tearDown(self):
        self._stop_server()
        self._tmp.cleanup()

    def test_non_object_body_gets_json_400_not_a_dropped_connection(self):
        """body 是合法 JSON 但不是对象（[] / 3 / "x" / null）时，
        以前 req.get() 抛 AttributeError，连接被断开、没有任何 JSON。
        """
        for path in ("/api/compose", "/api/grid2d", "/api/calib", "/api/fetch/start"):
            for body in ("[]", "3", '"x"', "null"):
                with self.subTest(path=path, body=body):
                    status, res = self._post_json(path, body)
                    self.assertEqual(status, 400)
                    self.assertFalse(res["ok"])
                    self.assertTrue(res["error"])

    def test_unparseable_body_gets_json_400(self):
        for path in ("/api/compose", "/api/grid2d", "/api/calib"):
            with self.subTest(path=path):
                status, res = self._post_json(path, "{not json")
                self.assertEqual(status, 400)
                self.assertFalse(res["ok"])

    def test_service_exception_becomes_json_500_not_a_dropped_connection(self):
        """统一异常边界：服务层漏出的异常也必须是 JSON。

        这条防的是"未知的畸形输入"——具体是哪个字段能触发不重要，重要的是
        任何异常都不会让前端拿到一个没有原因的失败。
        """
        boom = ValueError("模拟服务层异常")
        with mock.patch.object(MapService, "save_grid2d", side_effect=boom):
            status, res = self._post_json("/api/grid2d", {"map": "m", "zoom": "4", "data": {}})
        self.assertEqual(status, 500)
        self.assertFalse(res["ok"])
        self.assertIn("模拟服务层异常", res["error"])

    def test_normal_request_still_works(self):
        """边界不能把正常请求也变成错误。"""
        status, res = self._post_json("/api/grid2d", {
            "map": "m", "zoom": "4",
            "data": {"origin": [0, 0, 0], "cell_size": 1.0,
                     "cells": [[1, 1]], "blocked": []}})
        self.assertEqual(status, 200)
        self.assertTrue(res["ok"], res)


class ConditionalGetTest(_ServerMixin, unittest.TestCase):
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
        self._start_server(self.svc)

    def tearDown(self):
        self._stop_server()
        self._tmp.cleanup()

    def _check_revalidation(self, path):
        st, body, hdr = self._request(path)
        self.assertEqual(st, 200, path)
        self.assertTrue(body)
        etag = hdr.get("ETag")
        self.assertTrue(etag, f"{path} 没有 ETag")
        self.assertIn("max-age", hdr.get("Cache-Control", ""))
        st2, body2, _ = self._request(path, headers={"If-None-Match": etag})
        self.assertEqual(st2, 304, f"{path} 命中了 ETag 却回 {st2}")
        self.assertEqual(body2, b"", "304 不能带正文")

    def test_tile_revalidates_with_etag(self):
        self._check_revalidation("/tiles/m/4/0_0.png")

    def test_composite_revalidates_with_etag(self):
        self._check_revalidation("/maps/m/4/m_4.png")

    def test_etag_changes_when_content_changes(self):
        """ETag 必须跟着内容走：瓦片重抓后内容变了，不能还回 304。"""
        _, _, h1 = self._request("/tiles/m/4/0_0.png")
        p = self.tiles / "latest" / "m" / "4" / "0_0.png"
        p.write_bytes(b"different-bytes")
        st, body, _ = self._request("/tiles/m/4/0_0.png",
                                    headers={"If-None-Match": h1["ETag"]})
        self.assertEqual(st, 200, "内容变了却回了 304")
        self.assertEqual(body, b"different-bytes")


class StaticAssetsTest(_ServerMixin, unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self.svc = MapService(data_root=d, tiles_root=d / "tiles",
                              profile_dir=d / "p", grid2d_dir=d / "grids")
        self._start_server(self.svc)

    def tearDown(self):
        self._stop_server()
        self._tmp.cleanup()

    def test_page_references_split_assets(self):
        status, body, _ = self._request("/map")
        html = body.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("/static/css/map_composer.css", html)
        self.assertIn("/static/js/map_composer.js", html)
        self.assertNotIn("<style>", html)
        self.assertNotIn("<script>", html)
        self.assertIn('<script type="module" src="/static/js/map_composer.js"></script>', html)
        self.assertIn('data-mode="edit"', html)
        self.assertIn('id="sideTabs"', html)
        self.assertIn('id="layerGrid"', html)

    def test_css_and_javascript_are_served(self):
        status, css, headers = self._request("/static/css/map_composer.css")
        self.assertEqual(status, 200)
        self.assertIn("text/css", headers.get_content_type())
        self.assertIn("no-cache", headers.get("Cache-Control", ""))
        self.assertIn(b":root", css)

        status, js, headers = self._request("/static/js/map_composer.js")
        self.assertEqual(status, 200)
        self.assertIn("text/javascript", headers.get_content_type())
        self.assertIn(b"use strict", js)

        status, module, headers = self._request("/static/js/grid/grid-document.js")
        self.assertEqual(status, 200)
        self.assertIn("text/javascript", headers.get_content_type())
        self.assertIn(b"export class GridDocument", module)


if __name__ == "__main__":
    unittest.main()
