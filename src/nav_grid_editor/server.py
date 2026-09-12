#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HTTP 服务层：FastAPI 应用。

- ``/map``  地图瓦片实时采集/合成 + 标定 + 网格编辑 + 路径标记 + 取坐标
- ``/``     重定向到 ``/map``
- 全部接口的数据层在 map_service.py

由 cli.py 创建 MapService 并赋给本模块的 ``service`` 后启动；
测试里也可以直接对 ``server.app`` 起一个 uvicorn 实例来打。

为什么从 http.server 换成 FastAPI：
- **HTTP/1.1 keep-alive 是 uvicorn 自带的**。原来用 stdlib 时必须手工设
  ``protocol_version = "HTTP/1.1"``，否则 Chrome 每个请求新开 socket、其 socket
  池还会先等约 300ms——修之前实测同一张缩略图连取三次都是 310ms，服务端只要 3ms。
- 阻塞型处理（Playwright 抓取、PIL 合成、标记抓取）写成同步 ``def`` 端点即可，
  FastAPI 会自动丢到线程池，不会卡住事件循环。
- SSE 用 ``StreamingResponse``；静态/图片响应用 ``Response`` 自己带 ETag。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import queue
import re
import sys
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse

from .map_service import MapService

BASE_DIR = Path(__file__).resolve().parent
HOST = "127.0.0.1"

WEB_DIR = BASE_DIR / "web"
COMPOSER_FILE = WEB_DIR / "map_composer.html"

#: cli.py 中创建 MapService 后赋给它
service: MapService | None = None


def _svc() -> MapService:
    if service is None:  # pragma: no cover - 配置错误，正常路径不会发生
        raise RuntimeError("server.service 未初始化")
    return service


def _log(msg: str) -> None:
    sys.stderr.write(f"[nav-grid-editor] {msg}\n")


# =========================================================
# 响应小工具
# =========================================================

def _image_type(path: Path) -> str:
    """按后缀给 Content-Type（缩略图有 WebP / PNG 两种可能）。"""
    return "image/webp" if path.suffix.lower() == ".webp" else "image/png"


def _etag_of_bytes(data: bytes) -> str:
    """按内容算 ETag（blake2b 8 字节，30KB 瓦片约 0.05ms）。

    不用路径+mtime：瓦片是按坐标寻址的，同坐标的内容会在重抓后变化，
    ETag 必须跟着内容走，否则会拿旧内容回 304。
    """
    return '"' + hashlib.blake2b(data, digest_size=8).hexdigest() + '"'


def _cached(request: Request, data: bytes, media_type: str,
            max_age: int, etag: str) -> Response:
    """带 ETag 发字节；命中 If-None-Match 就回 304（不重传正文）。"""
    cache = f"private, max-age={max_age}, must-revalidate"
    headers = {"ETag": etag, "Cache-Control": cache}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=data, media_type=media_type, headers=headers)


def _cached_file(request: Request, path: Path, media_type: str, max_age: int) -> Response | None:
    """同 _cached，但直接从文件读。文件不存在返回 None（调用方给 404）。"""
    try:
        st = path.stat()
        data = path.read_bytes()
    except OSError:
        return None
    return _cached(request, data, media_type, max_age,
                   f'"{int(st.st_mtime_ns)}-{st.st_size}"')


def _bad_request(msg: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg}, status_code=400)


async def _json_body(request: Request) -> tuple[dict, str | None]:
    """解析 JSON 请求体，返回 ``(对象, 错误信息)``。

    必须确认结果是 dict 才能用：body 是合法 JSON 的 ``[]`` / ``3`` / ``"x"`` /
    ``null`` 时，直接 ``req.get(...)`` 会抛 AttributeError，连接被断开且没有任何
    JSON 响应，前端只看到请求失败、日志里多一段堆栈。
    """
    raw = await request.body()
    try:
        req = json.loads(raw.decode("utf-8") or "{}")
    except Exception as e:  # noqa: BLE001
        return {}, f"请求体解析失败: {e}"
    if not isinstance(req, dict):
        return {}, "请求体必须是 JSON 对象"
    return req, None


# =========================================================
# 应用与路由
# =========================================================

app = FastAPI(title="nav-grid-editor", docs_url="/docs", redoc_url=None)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    """统一异常边界：服务层漏出的异常也要变成一条 JSON。

    前端一律 ``await resp.json()``；异常直接冒到 ASGI 层的话，前端只拿到一个
    没有原因的失败，日志里只有一段堆栈。
    """
    _log(f"{request.method} {request.url.path} 异常: {exc!r}")
    return JSONResponse({"ok": False, "error": f"服务端异常: {exc}"}, status_code=500)


# ---------------- 页面 ----------------

@app.get("/")
@app.get("/index.html")
def index() -> RedirectResponse:
    return RedirectResponse("/map", status_code=302)


@app.get("/map")
def map_page() -> Response:
    if not COMPOSER_FILE.exists():  # pragma: no cover
        return Response("map_composer.html not found", status_code=404,
                        media_type="text/plain; charset=utf-8")
    # no-store：这是开发期经常改的页面，别让浏览器缓存住旧版
    return Response(COMPOSER_FILE.read_bytes(), media_type="text/html; charset=utf-8",
                    headers={"Cache-Control": "no-store"})


# ---------------- 查询类接口 ----------------

@app.get("/api/tilemaps")
def tilemaps() -> JSONResponse:
    return JSONResponse({"maps": _svc().store.list_maps()})


@app.get("/api/maps")
def maps_info(map: str = "", zoom: str = "") -> JSONResponse:
    info = _svc().saved_map_info(map, zoom)
    return JSONResponse({"ok": True, **info} if info else {"ok": True, "file": None})


@app.get("/api/calib")
def calib_get(map: str = "", zoom: str = "") -> JSONResponse:
    data = _svc().get_calib(map, zoom)
    return JSONResponse({"ok": True, **data} if data else {"ok": True, "file": None})


@app.get("/api/grid2d")
def grid2d_get(map: str = "", zoom: str = "") -> JSONResponse:
    return JSONResponse({"ok": True, **_svc().get_grid2d(map, zoom)})


@app.get("/api/fetch/status")
def fetch_status() -> JSONResponse:
    return JSONResponse(_svc().status()["fetch"])


@app.get("/api/marks/status")
def marks_status() -> JSONResponse:
    return JSONResponse(_svc().marks_status())


# ---------------- 图片：都带 ETag / 304 ----------------

@app.get("/api/overview")
def overview(request: Request, map: str = "", zoom: str = "") -> Response:
    p = _svc().ensure_overview(map, zoom)
    if not p:
        return Response("overview not found", status_code=404,
                        media_type="text/plain; charset=utf-8")
    # max-age=0 + ETag：合成会重新生成缩略图，所以不能让浏览器缓存太久
    # （否则重合成之后还看旧图）。但它只是一个请求，走 304 重验极便宜。
    # 缩略图是 WebP（PNG 的 1/12 大），Content-Type 按后缀给。
    resp = _cached_file(request, p, _image_type(p), max_age=0)
    return resp or Response("overview not found", status_code=404,
                            media_type="text/plain; charset=utf-8")


@app.get("/tiles/{map_name}/{zoom}/{filename}")
def tile(request: Request, map_name: str, zoom: str, filename: str) -> Response:
    # 限长 7 位：坐标本来就被 serve_tile 限制在 ±1_000_000，而不限长的话一个
    # 几千位的数字串会让 int() 抛 ValueError（Python 3.12 对 int(str) 有 4300
    # 位上限）
    m = re.fullmatch(r"(-?\d{1,7})_(-?\d{1,7})\.png", filename)
    if not m:
        return Response("Not Found", status_code=404, media_type="text/plain; charset=utf-8")
    data = _svc().serve_tile(map_name, zoom, int(m.group(1)), int(m.group(2)))
    if data is None:
        return Response("tile not found", status_code=404,
                        media_type="text/plain; charset=utf-8")
    # 瓦片按坐标寻址、内容只在重抓后才变。以前 max-age=30 且没有 ETag，等于浏览
    # 过程中每 30 秒把视野里那几百张瓦片全部重下一遍；现在 5 分钟有效期 + 内容
    # ETag：有效期内一个请求都不发，过期后也只回 304。
    return _cached(request, data, "image/png", max_age=300, etag=_etag_of_bytes(data))


@app.get("/maps/{rest:path}")
def saved_map(request: Request, rest: str) -> Response:
    root = _svc().store.tiles_root / "maps"
    cands: list[Path] = []
    # 新结构: maps/<地图>/<zoom>/<文件>.png（只保留一份）
    nested = re.fullmatch(r"([A-Za-z0-9_\-]+)/(\d+)/([A-Za-z0-9_\-]+\.png)", rest)
    if nested:
        cands.append(root / nested.group(1) / nested.group(2) / nested.group(3))
    # 旧结构: maps/<文件>.png（兼容早期拼接输出）
    flat = re.fullmatch(r"([A-Za-z0-9_\-]+\.png)", rest)
    if flat:
        cands.append(root / flat.group(1))
    for p in cands:
        if p.is_file():
            resp = _cached_file(request, p, "image/png", max_age=300)
            if resp:
                return resp
    return Response("map not found", status_code=404, media_type="text/plain; charset=utf-8")


# ---------------- SSE ----------------

@app.get("/api/events")
def events(request: Request) -> StreamingResponse:
    svc = _svc()

    async def stream():
        q = svc.bus.subscribe()
        try:
            hello = json.dumps({"type": "hello", "service": svc.status()}, ensure_ascii=False)
            yield f"data: {hello}\n\n"
            while True:
                try:
                    # 放到线程里等：bus 是同步 queue.Queue，直接阻塞会卡住事件循环。
                    # 客户端断开时 Starlette 会取消这个生成器，finally 里退订。
                    text = await asyncio.to_thread(q.get, True, 1.0)
                except queue.Empty:
                    continue
                yield f"data: {text}\n\n"
        finally:
            svc.bus.unsubscribe(q)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream; charset=utf-8",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


# ---------------- 动作类接口（同步实现，FastAPI 会丢到线程池） ----------------

@app.post("/api/fetch/start")
async def fetch_start(request: Request) -> Response:
    req, err = await _json_body(request)
    if err:
        return _bad_request(err)
    return JSONResponse(_svc().start_fetch(headless=bool(req.get("headless", False))))


@app.post("/api/fetch/stop")
def fetch_stop() -> JSONResponse:
    return JSONResponse(_svc().stop_fetch())


@app.post("/api/compose")
async def compose(request: Request) -> Response:
    req, err = await _json_body(request)
    if err:
        return _bad_request(err)
    return JSONResponse(_svc().start_compose(
        str(req.get("map", "")), str(req.get("zoom", "")),
        save=bool(req.get("save", False)),
    ))


@app.post("/api/marks/fetch")
async def marks_fetch(request: Request) -> Response:
    req, err = await _json_body(request)
    if err:
        return _bad_request(err)
    return JSONResponse(_svc().start_marks(str(req.get("kind", "public"))))


@app.post("/api/simulate/start")
def simulate_start() -> JSONResponse:
    return JSONResponse(_svc().start_simulate())


@app.post("/api/simulate/stop")
def simulate_stop() -> JSONResponse:
    return JSONResponse(_svc().stop_simulate())


@app.post("/api/coords")
async def coords(request: Request) -> JSONResponse:
    return JSONResponse(_svc().relay_coords(await request.body()))


@app.post("/api/calib")
async def calib_save(request: Request) -> Response:
    req, err = await _json_body(request)
    if err:
        return _bad_request(err)
    if "threshold" in req:
        try:
            req["threshold"] = float(req["threshold"])
        except (TypeError, ValueError):
            req["threshold"] = 5.0
    return JSONResponse(_svc().save_calib(
        str(req.get("map", "")), str(req.get("zoom", "")),
        req.get("points", []), req.get("image_size", {}),
        threshold=req.get("threshold", 5.0),
    ))


@app.post("/api/grid2d")
async def grid2d_save(request: Request) -> Response:
    req, err = await _json_body(request)
    if err:
        return _bad_request(err)
    return JSONResponse(_svc().save_grid2d(
        str(req.get("map", "")), str(req.get("zoom", "")),
        req.get("data", {}),
    ))
