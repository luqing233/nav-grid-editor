#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nav-grid-editor 的唯一启动入口。

用法::

    nav-grid-editor [--port 8765] [--data-root PATH]
    nav-grid-editor fetch-marks [抓取参数]
    python -m nav_grid_editor [同上]

页面：

- ``/``     自动重定向到 ``/map``
- ``/map``  地图瓦片实时采集/合成 + 标定 + 2D 网格编辑 + 路径标记 + 取坐标

地图接口（路由见 ``api/app.py``，数据层见 ``services/maps/``）：

- GET  /api/tilemaps                      列出瓦片库里的地图与 zoom
- GET  /api/maps?map=&zoom=               已保存总图信息（供下拉框瞬时显示）
- GET  /api/overview?map=&zoom=           缩略总图（长边 <=2048，秒开用，按需生成并缓存）
- GET  /api/calib?map=&zoom=              读取标定（像素↔游戏坐标）
- POST /api/calib                         计算并保存标定
- GET  /api/grid2d?map=&zoom=             读取 2D 网格（无高度）
- POST /api/grid2d                        保存 2D 网格（<地图>_<zoom>.grid.npz，稠密 uint8）
- GET  /api/events                        SSE 事件流（tile/stats/log/coords/hello）
- GET  /tiles/<地图>/<zoom>/<x>_<y>.png   瓦片图片
- GET  /maps/<地图>/<zoom>/<文件>.png     已拼接总图
- POST /api/fetch/start                   开始 Playwright 抓取 {"headless": bool}
- POST /api/fetch/stop                    停止抓取，保存清单并合并到 latest
- POST /api/compose                       合成并流式推送 {"map","zoom","save": bool}
- POST /api/simulate/start|stop           模拟抓取（不落盘，演示实时贴图）
- POST /api/coords                        坐标中继（可选 ws://127.0.0.1:3001 同功能）

数据根目录默认取**当前工作目录**。采集/编辑产物统一收在它下面的 ``assets/``：
``assets/tiles/``（瓦片与合成总图）、``assets/maps/``（2D 网格）、
``assets/items/``（地图标记数据）。``browser_profile/``（登录态）与
``configs/``（设备ID）是凭证类目录，刻意留在 ``assets/`` 外面。

可用 ``--data-root`` / ``NAV_DATA_ROOT`` 换根目录，或用 ``--tiles-root`` /
``--profile-dir`` / ``--grid2d-dir`` 单独覆盖。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
from pathlib import Path

from .api import app as api_app
from .api.app import HOST
from .services import maps as map_service
from .services.maps import MapService

DEFAULT_PORT = 8765


# ---------------- 可选：WebSocket 坐标中继（与 wsserver/main.py 兼容） ----------------

def start_ws_relay(svc: MapService, port: int = 3001):
    """如果装有 websockets，就在 3001 端口额外开一个中继，兼容同一数据链路。"""
    try:
        import websockets  # noqa: F401
    except Exception:
        print("未安装 websockets：坐标中继 ws://127.0.0.1:3001 跳过；"
              "仍可用 POST /api/coords 上报坐标（页面 /map 实时显示）")
        return

    clients = set()

    async def handler(ws):
        clients.add(ws)
        print("ws-coords: browser connected")
        try:
            async for msg in ws:
                try:
                    data = json.loads(msg)
                    pos = data.get("data", {}).get("pos", {})
                    print(f"[coords] x={pos.get('x')} z={pos.get('z')}")
                    if isinstance(data, dict) and "data" in data:
                        data = data["data"]  # 拍平，与 POST /api/coords 一致
                except Exception:
                    data = msg
                # 推给网页（SSE）
                svc.bus.emit(type="coords", data=data)
                # 广播给所有 ws 客户端（含发送者）
                dead = []
                for c in clients:
                    try:
                        await c.send(msg)
                    except Exception:
                        dead.append(c)
                for c in dead:
                    clients.discard(c)
        finally:
            clients.discard(ws)

    async def serve():
        async with websockets.serve(handler, "127.0.0.1", port):
            await asyncio.Future()

    t = threading.Thread(target=lambda: asyncio.run(serve()),
                         name="ws-coords-relay", daemon=True)
    t.start()
    print(f"坐标中继已启动: ws://127.0.0.1:{port}")


# ---------------- 入口 ----------------

def _run_server(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="nav-grid-editor",
        description="地图瓦片采集/合成 + 标定 + 2D 网格编辑 统一服务",
        epilog="附加命令: nav-grid-editor fetch-marks（用账号凭证抓取地图标记）",
    )
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--data-root", default="",
                    help="数据根目录（assets/ browser_profile/ configs/ 的父目录，"
                         "默认当前工作目录）")
    ap.add_argument("--tiles-root", default="", help="瓦片根目录（默认 <data-root>/assets/tiles）")
    ap.add_argument("--profile-dir", default="",
                    help="浏览器登录配置目录（默认 <data-root>/browser_profile）")
    ap.add_argument("--grid2d-dir", default="",
                    help="2D 网格输出目录（默认 <data-root>/assets/maps，"
                         "可用环境变量 NAV_GRID2D_DIR）")
    args = ap.parse_args(argv)

    svc = MapService(
        data_root=Path(args.data_root) if args.data_root else None,
        tiles_root=Path(args.tiles_root) if args.tiles_root else None,
        profile_dir=Path(args.profile_dir) if args.profile_dir else None,
        grid2d_dir=Path(args.grid2d_dir) if args.grid2d_dir else None,
    )
    # 把服务实例挂到 server 模块，路由直接使用（与原来挂给 Handler 的用法一致）
    api_app.service = svc

    print(f"地图采集/编辑页面:       http://{HOST}:{args.port}/（自动跳转 /map）")
    print(f"数据根目录:              {svc.data_root}")
    print(f"瓦片目录:                {svc.store.tiles_root}")
    print(f"2D 网格目录:             {svc.grid2d_dir}")
    print(f"浏览器登录配置:          {svc.profile_dir}")
    if not map_service.PLAYWRIGHT_OK:
        print("提示: 未安装 playwright，抓取功能不可用"
              "（uv sync && uv run playwright install chromium）")
    if map_service.Image is None:
        print("提示: 未安装 Pillow，WebP 瓦片转换与总图落盘不可用")

    start_ws_relay(svc)

    # uvicorn 自带 HTTP/1.1 keep-alive（原来用 stdlib 得手工设 protocol_version，
    # 否则 Chrome 每个请求新开 socket、还要在 socket 池里等约 300ms）。
    # access_log 关掉：瓦片请求是按张计的，几百张一屏会把控制台刷爆；
    # 应用自己的 [map] / [nav-grid-editor] 日志保留。
    import uvicorn

    print("按 Ctrl+C 退出")
    try:
        uvicorn.run(api_app.app, host=HOST, port=args.port,
                    log_level="warning", access_log=False)
    except KeyboardInterrupt:
        print("\n已退出")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "fetch-marks":
        from .commands.fetch_marks import main as fetch_marks_main

        return fetch_marks_main(args[1:])
    return _run_server(args)


if __name__ == "__main__":
    raise SystemExit(main())
