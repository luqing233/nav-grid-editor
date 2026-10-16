# -*- coding: utf-8 -*-
"""终末地官方地图 WebSocket 位置客户端。

移植自 ok-end-field（AGPL-3.0）的 ``src/localization/ws_position_mixin.py``，
只保留「官方地图 WS 客户端」这一条链路（本地 3001 中继模式已不需要）。

链路与 :mod:`nav_grid_editor.services.marks` 共用同一套认证：

    hg/check content → oauth code → dId → cred/token
        → GET /api/v1/websocket/token → wss://ws.skland.com/.../endfield/map

连上官方地图 WS 后：

* ``type=1`` 发 token 鉴权，``type=2`` 是鉴权成功；
* 鉴权后每 10 秒发 ``type=3`` 心跳、每 5 秒发 ``type=1011`` 角色初始化/刷新；
* ``type=6``（``code=10002``）表示 token 过期，重新取 token 后再发 ``type=1``；
* ``type=1012`` 是位置推送，形如 ``{"data": {"pos": {x,y,z}, "mapId", "levelId"}}``。

收到位置后通过 ``on_position`` 回调交给上层（``MapService`` 再广播到 SSE），
网页因此能直接画玩家位置，不必再依赖本地 ``ws://127.0.0.1:3001`` 中继。
"""

from __future__ import annotations

import inspect
import json
import random
import threading
import time
from typing import Any, Callable

from websockets.sync.client import connect as _ws_connect

from . import marks as marks_mod

ENDFIELD_MAP_WS_URL = "wss://ws.skland.com/ws/v1/game/endfield/map"

_ORIGIN = "https://game.skland.com"
_REFERER = "https://game.skland.com/map/endfield"
_WS_UA = "Mozilla/5.0 ok-ef map websocket client"

_AUTH_RETRY_SECONDS = 30.0     # 凭证缺失/认证失败后的重试间隔
_RECONNECT_SECONDS = 30.0      # 连接中断后的重连间隔
_HEARTBEAT_SECONDS = 10.0      # 鉴权后心跳
_INIT_SECONDS = 5.0            # 鉴权后角色初始化/刷新
_RECV_TIMEOUT_SECONDS = 1.0    # 收包轮询间隔（同时决定 stop 响应速度）

Log = Callable[[str], None]
OnPosition = Callable[[dict], None]


def _noop(_msg: str) -> None:
    pass


def _make_msg_id() -> str:
    """生成协议消息 ID：仅用于请求-响应匹配，非安全用途。"""
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return "".join(random.choice(alphabet) for _ in range(8))


def extract_position(payload: Any) -> tuple[dict | None, str | None, float, float, float]:
    """从 WS 消息里取位置，返回 ``(pos, map_id, x, y, z)``；无效时全为 None/0。

    兼容 ``{"data": {"pos": {...}, "mapId"}}``、``{"data": {x,y,z,...}}``
    和顶层 ``{x,y,z,...}`` 三种形态（与 ok-end-field 一致）。
    """
    if not isinstance(payload, dict):
        return None, None, 0.0, 0.0, 0.0

    data = payload.get("data")
    if isinstance(data, dict):
        pos = data.get("pos")
        if isinstance(pos, dict):
            x, y, z = pos.get("x"), pos.get("y"), pos.get("z")
            if x is not None and y is not None and z is not None:
                map_id = data.get("mapId") or data.get("levelId")
                if map_id is None:
                    return None, None, 0.0, 0.0, 0.0
                return pos, str(map_id), float(x), float(y), float(z)
        if all(k in data for k in ("x", "y", "z")):
            map_id = data.get("mapId") or data.get("levelId")
            if map_id is None:
                return None, None, 0.0, 0.0, 0.0
            return data, str(map_id), float(data["x"]), float(data["y"]), float(data["z"])

    if all(k in payload for k in ("x", "y", "z")):
        map_id = payload.get("mapId") or payload.get("levelId")
        if map_id is None:
            return None, None, 0.0, 0.0, 0.0
        return (payload, str(map_id),
                float(payload["x"]), float(payload["y"]), float(payload["z"]))

    return None, None, 0.0, 0.0, 0.0


def _connect_ws(url: str, headers: dict[str, str]):
    """按 websockets 版本选择握手头参数名（新版本 additional_headers，旧版本 extra_headers）。"""
    params = inspect.signature(_ws_connect).parameters
    kwargs: dict[str, Any] = {"open_timeout": 15.0, "ping_interval": None}
    if "additional_headers" in params:
        kwargs["additional_headers"] = headers
    elif "extra_headers" in params:
        kwargs["extra_headers"] = headers
    return _ws_connect(url, **kwargs)


class EndfieldPositionClient:
    """后台线程里的官方地图 WS 客户端：拉取玩家实时位置并回调。

    只依赖 :func:`marks.read_hg_content` 读到的凭证，不监听任何本地端口。
    """

    def __init__(self, on_position: OnPosition, log: Log | None = None,
                 on_state: Callable[[dict], None] | None = None,
                 content_reader: Callable[[], str] | None = None,
                 ws_url: str = ENDFIELD_MAP_WS_URL):
        self._on_position = on_position
        self._on_state = on_state
        self._log = log or _noop
        self._read_content = content_reader or marks_mod.read_hg_content
        self._ws_url = ws_url

        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._connected = False
        self._credential_ready = False
        self._role: dict | None = None
        self._map_id: str | None = None
        self._level_id: str | None = None
        self._last_position: dict | None = None
        self._last_position_at = 0.0
        self._last_error: str | None = None
        self._state_sig: tuple | None = None
        self._last_logged_error: str | None = None

    # ---------- 生命周期 ----------

    def running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def start(self) -> dict:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {"ok": True, "started": False, "reason": "already_running"}
            self._stop = threading.Event()
            self._thread = threading.Thread(
                target=self._run, name="EndfieldMapWs", daemon=True)
            self._thread.start()
        return {"ok": True, "started": True}

    def stop(self) -> dict:
        with self._lock:
            thread = self._thread
        self._stop.set()
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        return {"ok": True, "running": self.running()}

    def status(self) -> dict:
        with self._lock:
            running = bool(self._thread and self._thread.is_alive())
            if self._connected:
                hint = ""
            elif not self._credential_ready:
                hint = "缺凭证：需要 configs/hg_content.txt"
            elif self._last_error:
                hint = self._last_error
            else:
                hint = "连接中…" if running else "未开启"
            return {
                "running": running,
                "connected": self._connected,
                "source": "ws",
                "hint": hint,
                "credential_ready": self._credential_ready,
                "role": dict(self._role) if self._role else None,
                "map_id": self._map_id,
                "level_id": self._level_id,
                "last_position": dict(self._last_position) if self._last_position else None,
                "last_position_at": self._last_position_at,
                "last_error": self._last_error,
            }

    # ---------- 内部：状态 ----------

    def _set_state(self, **kw) -> None:
        """按 ``connected`` → ``self._connected`` 的约定更新状态字段。"""
        with self._lock:
            for key, value in kw.items():
                setattr(self, "_" + key, value)
            sig = (self._connected, self._credential_ready, self._map_id,
                   self._level_id, self._last_error, bool(self._last_position))
        if self._on_state and sig != self._state_sig:
            self._state_sig = sig
            try:
                self._on_state(self.status())
            except Exception:
                pass

    def _emit(self, msg: str) -> None:
        try:
            self._log(msg)
        except Exception:
            pass

    def _wait(self, seconds: float) -> bool:
        """可被 ``stop()`` 打断的等待；返回 False 表示收到停止信号。"""
        deadline = time.time() + max(0.0, seconds)
        while time.time() < deadline:
            if self._stop.wait(0.25):
                return False
        return not self._stop.is_set()

    # ---------- 内部：主循环 ----------

    def _run(self) -> None:
        self._emit("[地图WS] 位置客户端启动")
        while not self._stop.is_set():
            try:
                self._run_session()
            except Exception as e:  # noqa: BLE001 — 线程里必须兜住，断线要能自动重连
                if self._stop.is_set():
                    break
                err_text = str(e)
                self._set_state(connected=False, last_error=err_text)
                if err_text != self._last_logged_error:
                    self._last_logged_error = err_text
                    self._emit(f"[地图WS] 连接中断，{int(_RECONNECT_SECONDS)} 秒后重试: {err_text}")
                self._wait(_RECONNECT_SECONDS)
        self._set_state(connected=False)
        self._emit("[地图WS] 位置客户端已停止")

    def _run_session(self) -> None:
        content = str(self._read_content() or "").strip()
        if not content:
            err_text = "未配置凭证（configs/hg_content.txt）"
            self._set_state(credential_ready=False, connected=False, last_error=err_text)
            if err_text != self._last_logged_error:
                self._last_logged_error = err_text
                self._emit(f"[地图WS] 找不到凭证，{int(_AUTH_RETRY_SECONDS)} 秒后重试："
                           "请把 hg/check 响应的 data.content 粘到 configs/hg_content.txt")
            self._wait(_AUTH_RETRY_SECONDS)
            return

        self._set_state(credential_ready=True)
        self._emit("[地图WS] 换取认证（oauth → cred/token）…")
        auth = marks_mod.exchange_content(content, self._emit)
        self._last_logged_error = None  # 认证通了，下次出错重新提示
        client = marks_mod.SignedClient(auth)

        role = self._resolve_role(client)
        self._set_state(role=dict(role))
        self._emit(f"[地图WS] 已解析角色 serverId={role['serverId']} roleId={role['roleId']}")

        token = self._fetch_ws_token(client)
        headers = {
            "User-Agent": _WS_UA,
            "Accept": "application/json, text/plain, */*",
            "Origin": _ORIGIN,
            "Referer": _REFERER,
            "cred": str(auth.get("cred") or ""),
        }
        with _connect_ws(self._ws_url, headers) as ws:
            self._set_state(connected=True, last_error=None)
            self._emit(f"[地图WS] 已连接: {self._ws_url}")

            self._send(ws, 1, {"token": token})
            auth_ok = False
            last_heartbeat = 0.0
            last_init = 0.0

            while not self._stop.is_set():
                now = time.time()
                if auth_ok and now - last_heartbeat >= _HEARTBEAT_SECONDS:
                    self._send(ws, 3, {})
                    last_heartbeat = now
                if auth_ok and now - last_init >= _INIT_SECONDS:
                    self._send(ws, 1011, dict(role))
                    last_init = now

                try:
                    raw = ws.recv(timeout=_RECV_TIMEOUT_SECONDS)
                except TimeoutError:
                    continue
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8", errors="ignore")
                if not isinstance(raw, str) or not raw.strip().startswith("{"):
                    continue

                try:
                    payload = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(payload, dict):
                    continue

                msg_type = payload.get("type")
                if msg_type == 2:
                    auth_ok = True
                    last_init = 0.0
                    self._emit("[地图WS] 鉴权成功")
                    continue
                if msg_type == 6 and (payload.get("data") or {}).get("code") == 10002:
                    self._emit("[地图WS] websocket token 过期，重新获取…")
                    token = self._fetch_ws_token(client)
                    auth_ok = False
                    self._send(ws, 1, {"token": token})
                    continue
                if msg_type == 1012:
                    self._handle_position(payload)

    # ---------- 内部：认证与协议 ----------

    @staticmethod
    def _resolve_role(client: marks_mod.SignedClient) -> dict:
        binding = client.get("/api/v1/game/player/binding")
        roles = marks_mod.collect_roles(binding)
        if not roles:
            raise RuntimeError("该账号没有终末地角色（roleId/serverId）")
        return roles[0]

    @staticmethod
    def _fetch_ws_token(client: marks_mod.SignedClient) -> str:
        resp = client.get("/api/v1/websocket/token")
        if not isinstance(resp, dict) or resp.get("code") != 0:
            raise RuntimeError(f"获取 websocket token 失败: {resp}")
        token = str(((resp.get("data") or {}).get("token")) or "").strip()
        if not token:
            raise RuntimeError("websocket token 为空")
        return token

    @staticmethod
    def _send(ws, msg_type: int, data: dict | None = None) -> None:
        payload = {"type": msg_type, "data": data or {}, "msgId": _make_msg_id()}
        ws.send(json.dumps(payload))

    def _handle_position(self, payload: dict) -> None:
        # 整包打印是**刻意**留的诊断：官方 WS 的字段会随版本变（isOnline、
        # levelId 之类），楼层判定出问题时只能靠原始报文比对。位置流默认关闭、
        # 由用户在页面手动开，且日志面板只保留最近 400 行，所以不会失控。
        try:
            self._emit("[地图WS] 完整位置消息: " +
                       json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        except Exception:
            pass
        pos, map_id, x, y, z = extract_position(payload)
        if pos is None or map_id is None:
            return
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        raw_level_id = data.get("levelId") or payload.get("levelId")
        level_id = str(raw_level_id).strip() if raw_level_id is not None else None
        self._set_state(map_id=map_id, level_id=level_id,
                        last_position={"x": x, "y": y, "z": z,
                                       "mapId": map_id, "levelId": level_id},
                        last_position_at=time.time())
        try:
            self._on_position(data)
        except Exception as e:  # noqa: BLE001 — 回调失败不能拖垮 WS 线程
            self._emit(f"[地图WS] 位置回调异常: {e}")


__all__ = ["ENDFIELD_MAP_WS_URL", "EndfieldPositionClient", "extract_position"]
