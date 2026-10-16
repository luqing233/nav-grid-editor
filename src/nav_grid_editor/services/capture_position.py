# -*- coding: utf-8 -*-
"""抓包后端位置源（endfield-capture-backend）。

把 ``GET http://127.0.0.1:<port>/api/state`` 轮询到的 ``lastPosition`` 翻译成
``MapService`` 需要的位置事件（``{"pos": {x, y, z}, "mapId", "levelId", ...}``）。
只用标准库 ``urllib``，不新增任何依赖，也不做任何抓包 / 解密。

为什么默认用它而不是官方地图 WS：抓包后端给的是**真实高度 y**、朝向 yaw 与速度
speed，楼层判定正需要 y；官方 WS 那条依赖 ``configs/hg_content.txt`` 里的账号凭证
（本机那份已经 401 过期），字段还随官方版本漂移。官方 WS 实现保留在
``endfield_ws.py``，设 ``NAV_POSITION_SOURCE=ws`` 可以切回去。

契约（见 endfield-capture-backend/docs/ok-end-field-integration.md）：

* ``lastPosition`` 一旦有值就**永远保留**（重登录、退游戏都不清空），所以要靠
  ``flows[]`` 里那条连接的 ``status`` / ``lastPacket`` 判断坐标是否还属于一局活着
  的连接，而不是看 ``lastPosition`` 自己。``flows[]`` 只返回最近 12 条，顶层
  ``status`` 是服务级状态、与连接无关；
* 上报是**事件驱动**的：移动中约 0.27 秒一条，**完全静止时没有位置包**。所以不能
  拿「多久没更新」当断线依据（静止会被误判成掉线），只能用连接活性；
  :func:`age_seconds` 只用于展示与提示；
* 后端**必须先于游戏主连接启动**，中途起来的那条流永远报「缺少连接起点」，
  这时重启后端也没用、只能重开游戏。本应用只读状态，绝不重启 / 拉起后端；
* 后端没有健康检查、没有重置端点。
"""

from __future__ import annotations

import json
import math
import threading
import time
from typing import Any, Callable
from urllib import error, request
from urllib.parse import urlsplit

CAPTURE_SOURCE_NAME = "capture"

DEFAULT_CAPTURE_PORT = 8765

#: 自动探测的端口区间（含两端）：``8765 → 8775``。
#: 后端默认占 8765，本服务默认也占 8765，所以两个都跑时后端通常被挪到 8766 往后；
#: 从 8765 按升序扫一遍，8765 优先，其次 8766、8767……
CAPTURE_PORT_START = DEFAULT_CAPTURE_PORT
CAPTURE_PORT_END = 8775

STATE_PATH = "/api/state"

#: 产出坐标的连接这么久没再收到**任何**报文，就认为这一局已经结束了。
#:
#: 后端的 snapshot 会把状态计算覆盖成「等待 TCP 补包」，那会遮住「连接已结束」这个
#: 终态，所以只看 status 的终态接不住正常结束的连接；而活着的游戏连接不会这么久
#: 完全静默（静止时也还有保活与其他对象的报文）。取 30 秒的理由见后端文档：误判
#: 「还活着」只是让陈旧窗口变长，误判「已死」却会把正常定位停掉。
FLOW_IDLE_SECONDS = 30.0

#: 连接终态的**确切**取值。
TERMINAL_FLOW_STATUSES = frozenset({"连接已结束"})

#: 「抓包晚于该连接」的终态前缀（完整取值是 ``缺少连接起点，请重新启动游戏``）。
NO_CONNECTION_START_PREFIX = "缺少连接起点"

#: 连接终态的**前缀**（「解码停止」后面还拼了具体原因，只能按前缀判）。
TERMINAL_FLOW_STATUS_PREFIXES = ("解码停止", NO_CONNECTION_START_PREFIX)

# 会话健康状态（``parse_health`` 的返回）。
HEALTH_BACKEND_DOWN = "backend_down"
HEALTH_NO_CONNECTION = "no_connection"
HEALTH_HANDSHAKE_PENDING = "handshake_pending"
HEALTH_DECODING = "decoding"
HEALTH_MISSED_HANDSHAKE = "missed_handshake"

Log = Callable[[str], None]
OnPosition = Callable[[dict], None]


def _noop(_msg: str) -> None:
    pass


def default_capture_url(port: int = DEFAULT_CAPTURE_PORT) -> str:
    return f"http://127.0.0.1:{int(port)}"


def capture_port_candidates(start: int = CAPTURE_PORT_START,
                            end: int = CAPTURE_PORT_END) -> list[int]:
    """自动探测的端口顺序：``8765, 8766, … 8775``（升序，含两端）。

    顺序即优先级——第一个答得像抓包后端的端口被采用。
    """
    low, high = int(start), int(end)
    if high < low:
        low, high = high, low
    return list(range(low, high + 1))


def capture_port_range_label(start: int = CAPTURE_PORT_START,
                             end: int = CAPTURE_PORT_END) -> str:
    """端口范围的可读标签，例如 ``8765–8775``。"""
    ports = capture_port_candidates(start, end)
    return f"{ports[0]}–{ports[-1]}"


def url_port(url: str) -> int | None:
    """取出 URL 里的端口；没有 / 非法返回 None。"""
    try:
        return urlsplit(str(url or "")).port
    except ValueError:
        return None


def looks_like_capture_state(payload: Any) -> bool:
    """这份 JSON 像不像抓包后端的 ``/api/state``。

    只认 ``flows`` / ``lastPosition`` 这两个一定有（哪怕为空 / 为 null）的键。**不能
    只看端口有没有人应答**：本服务自己默认也监听 8765，会对 ``/api/state`` 回 404，
    扫描时要把这种「端口是开的、但不是后端」的情况排除掉。
    """
    return isinstance(payload, dict) and ("flows" in payload or "lastPosition" in payload)


def probe_capture_state(base_url: str, timeout: float = 0.4) -> dict | None:
    """探一个端口是不是抓包后端；不是 / 连不上返回 None。"""
    url = str(base_url or "").rstrip("/") + STATE_PATH
    try:
        with request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", errors="ignore"))
    except (error.URLError, TimeoutError, OSError, ValueError):
        return None
    return payload if looks_like_capture_state(payload) else None


def as_float(value: Any) -> float | None:
    """转 finite float；不是数字 / 非有限 / bool 都返回 None。"""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def parse_flow_id(value: Any) -> int | None:
    """连接号：只认数字（bool 不算），其余返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return int(value)
    return None


def flow_status_is_terminal(status: Any) -> bool:
    """这条连接的解码状态是不是终态（不会再喂数据了）。

    终态一旦进入就不会退出，按它扣信任是**精确**判据，不依赖时间阈值。
    """
    text = str(status or "").strip()
    return text in TERMINAL_FLOW_STATUSES or text.startswith(TERMINAL_FLOW_STATUS_PREFIXES)


def flow_status_is_missed_handshake(status: Any) -> bool:
    """这条连接是不是「抓包晚于它建立」。

    这是所有终态里**唯一能靠新建一条连接救回来**的那种：后端没看到 SYN，推不出
    密钥流，但后端本身没坏 —— 换一条连接、让它从头抓握手就行。`连接已结束`（正常
    结束）与 `解码停止`（这条流真出问题了）都不属于这一类，重建连接对它们没意义。
    """
    return str(status or "").strip().startswith(NO_CONNECTION_START_PREFIX)


def age_seconds(record: Any, now: float | None = None) -> float:
    """``lastPosition.time`` 距当前**墙钟**多少秒；没有时间戳返回 0。

    ``time`` 是后端记录的 Unix 墙钟，所以这里必须用同一套时钟相减；用单调钟去减
    会恒为负、被夹成 0，安全提示就完全失效了。未来时间戳同样夹成 0。
    """
    current = time.time() if now is None else float(now)
    capture_t = as_float(record.get("time")) if isinstance(record, dict) else None
    if capture_t is None or capture_t <= 0:
        return 0.0
    return max(0.0, current - capture_t)


def flow_liveness(state: Any, record: Any, now: float | None = None) -> tuple[int | None, str, bool]:
    """这条坐标是哪条连接解出来的，那条连接还在不在。

    返回 ``(flow_id, status, alive)``。**认不出来时一律当「活着」**：``flows`` 缺失
    （老版本后端）或记录里没有 ``flow`` 时，缺信息不等于会话已死 —— 没有证据就不扣
    信任，否则会把一条正常的定位链整段停掉。

    三条判据，任中一条即认为该连接不会再喂数据：

    1. ``flows`` 里查不到这个 id（后端只留最近 12 条，说明已经老到被挤出去）；
    2. 状态是终态（``连接已结束`` / ``解码停止：…`` / ``缺少连接起点…``）；
    3. 这条连接已经 :data:`FLOW_IDLE_SECONDS` 没收到任何报文。
    """
    flows = state.get("flows") if isinstance(state, dict) else None
    if not isinstance(flows, list):
        return None, "", True
    flow_id = parse_flow_id(record.get("flow")) if isinstance(record, dict) else None
    if flow_id is None:
        return None, "", True
    entry = next(
        (item for item in flows
         if isinstance(item, dict) and parse_flow_id(item.get("id")) == flow_id),
        None,
    )
    if entry is None:
        return flow_id, "", False
    status = str(entry.get("status") or "")
    if flow_status_is_terminal(status):
        return flow_id, status, False
    current = time.time() if now is None else float(now)
    last_packet = as_float(entry.get("lastPacket"))
    if last_packet is not None and current - last_packet > FLOW_IDLE_SECONDS:
        return flow_id, status, False
    return flow_id, status, True


def parse_health(state: Any, now: float | None = None) -> dict:
    """只看 ``flows[]``，回答「这条抓包链现在处于什么状态」。

    **不看坐标** —— 坐标缺失有好几种成因（后端刚起、握手没完成、抓包晚了），混在
    一起就没法决定该做什么。有条死连接 + 有条活连接要判成正常：后端把历史连接一直
    留着，拿旧的死连接去判断会让每一次正常抓包都被误判。

    返回 ``{"health", "detail", "flow_id", "flow_status"}``。
    """
    current = time.time() if now is None else float(now)
    flows = state.get("flows") if isinstance(state, dict) else None
    if not isinstance(flows, list) or not flows:
        return {"health": HEALTH_NO_CONNECTION, "detail": "后端还没看到游戏连接",
                "flow_id": None, "flow_status": ""}

    pending: dict | None = None
    missed: dict | None = None
    for item in flows:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "")
        flow_id = parse_flow_id(item.get("id"))
        if flow_status_is_missed_handshake(status):
            missed = missed or {"health": HEALTH_MISSED_HANDSHAKE, "detail": status,
                                "flow_id": flow_id, "flow_status": status}
            continue
        if flow_status_is_terminal(status):
            # 正常结束或解码失败：都是这条流的终局，但不代表需要重建连接
            continue
        last_packet = as_float(item.get("lastPacket"))
        if last_packet is not None and current - last_packet > FLOW_IDLE_SECONDS:
            continue
        if status.strip() == "等待登录初始化":
            pending = pending or {"health": HEALTH_HANDSHAKE_PENDING, "detail": status,
                                  "flow_id": flow_id, "flow_status": status}
            continue
        # 其余进行中的状态（正在解码 / 「等待 TCP 补包」这个覆盖值）都算已经握上手了
        return {"health": HEALTH_DECODING, "detail": status,
                "flow_id": flow_id, "flow_status": status}

    if missed is not None:
        return missed
    if pending is not None:
        return pending
    return {"health": HEALTH_NO_CONNECTION, "detail": "没有仍在继续的连接",
            "flow_id": None, "flow_status": ""}


def _ws_map_id(state: Any) -> str:
    """后端自带地图 WS 的观测地图名（可选字段，只在场景表给不出地图时兜底）。"""
    ws = state.get("ws") if isinstance(state, dict) else None
    last = ws.get("lastPosition") if isinstance(ws, dict) else None
    if not isinstance(last, dict):
        return ""
    return str(last.get("mapId") or "").strip()


def resolve_map_id(record: Any, state: Any = None) -> tuple[str, str]:
    """定地图名，返回 ``(map_id, source)``。顺序与「不猜」的边界都很重要。

    ``levelId`` 非空 ⟺ 这个场景**在场景表里**，于是 ``mapId`` 为空就有确切含义：
    **它不属于任何大地图**（试炼场 ``blackbox_*``、独立场景 ``indie_*`` 等）。

    1. 场景表给了大地图 → 用它；
    2. 场景在表里但没有大地图 → 看后端地图 WS 的观测；没有就返回空串。
       **绝不按坐标范围去猜**：这些场景有自己的一套坐标，猜出来的地图会让导航
       加载错网格、照着错坐标规划路线；
    3. 场景不在表里（``levelId`` 为空，多半是客户端更新后新增的场景）→ 地图 WS。
    """
    record = record if isinstance(record, dict) else {}
    declared = str(record.get("mapId") or "").strip()
    if declared:
        return declared, "scene_table"
    if str(record.get("levelId") or "").strip():
        return "", "no_parent_map"
    ws_map_id = _ws_map_id(state)
    if ws_map_id:
        return ws_map_id, "backend_ws"
    return "", "unknown"


def build_position(record: Any, state: Any = None) -> dict | None:
    """``lastPosition`` 记录 → ``MapService._on_position`` 要的事件 dict。

    坐标不全（缺 x/y/z 或不是有限数）时返回 ``None``。带上 ``mapId`` / ``levelId``
    是因为上游靠 ``mapId`` 找网格、靠坐标判楼层；``yaw`` / ``speed`` / ``scene``
    等字段一并带出，页面与日志都有用。
    """
    if not isinstance(record, dict):
        return None
    x, y, z = as_float(record.get("x")), as_float(record.get("y")), as_float(record.get("z"))
    if x is None or y is None or z is None:
        return None
    map_id, map_id_source = resolve_map_id(record, state)
    level_id = str(record.get("levelId") or "").strip()
    return {
        "pos": {"x": x, "y": y, "z": z},
        "mapId": map_id,
        "mapIdSource": map_id_source,
        "levelId": level_id,
        "yaw": as_float(record.get("yaw")),
        "speed": as_float(record.get("speed")),
        "time": as_float(record.get("time")),
        "scene": record.get("scene"),
        "levelName": str(record.get("levelName") or ""),
        "mapName": str(record.get("mapName") or ""),
        "dungeonName": str(record.get("dungeonName") or ""),
        "flow": parse_flow_id(record.get("flow")),
        "source": CAPTURE_SOURCE_NAME,
    }


def position_key(position: dict | None) -> tuple | None:
    """位置去重键：连接 + 抓包时刻 + 坐标。同一份坐标只上报一次。"""
    if not isinstance(position, dict):
        return None
    pos = position.get("pos") or {}
    return (
        position.get("flow"),
        position.get("time"),
        pos.get("x"), pos.get("y"), pos.get("z"),
    )


class CapturePositionClient:
    """轮询抓包后端的位置源，接口与 :class:`EndfieldPositionClient` 一致。

    ``start`` / ``stop`` / ``status`` + ``on_position`` / ``on_state`` 回调；内部是
    一条后台线程按 ``poll_interval`` 轮询 ``GET /api/state``。**只读**：不启动、不
    重启、不干预后端进程。
    """

    def __init__(self, on_position: OnPosition, log: Log | None = None,
                 on_state: Callable[[dict], None] | None = None,
                 base_url: str | None = None,
                 port_start: int = CAPTURE_PORT_START,
                 port_end: int = CAPTURE_PORT_END,
                 poll_interval: float = 0.5, http_timeout: float = 1.0,
                 scan_cooldown: float = 5.0):
        self._on_position = on_position
        self._on_state = on_state
        self._log = log or _noop
        self._poll_interval = max(0.05, float(poll_interval))
        self._http_timeout = max(0.1, float(http_timeout))

        # 给了 base_url 就钉死用那个（CLI / 环境变量显式指定的场合）；否则按
        # ``port_start → port_end`` 升序自动探测。
        self._fixed_url = str(base_url or "").strip().rstrip("/")
        self._port_start = int(port_start)
        self._port_end = int(port_end)
        self._candidates = [default_capture_url(port)
                            for port in capture_port_candidates(self._port_start, self._port_end)]
        self._active_url = self._fixed_url
        self._capture_port: int | None = url_port(self._fixed_url) if self._fixed_url else None
        #: 最近一次锁定的端口（不随「掉线 / 重新扫描」清空），用于把「连不上」说清是哪个口
        self._last_port: int | None = self._capture_port
        self._scan_cooldown = max(0.0, float(scan_cooldown))
        # 探端口用更短的超时：扫描要串行打 10 个口，不能每个都等满 http_timeout
        self._scan_timeout = min(self._http_timeout, 0.4)
        self._last_scan_at = 0.0

        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._connected = False
        self._flow_decoding = False
        self._health = HEALTH_BACKEND_DOWN
        self._hint = "未开启"
        self._detail = ""
        self._flow_id: int | None = None
        self._flow_status = ""
        self._position_alive = False
        self._last_position: dict | None = None
        self._last_position_at = 0.0
        self._last_error: str | None = None
        self._state_sig: tuple | None = None
        self._emitted_key: tuple | None = None
        self._last_logged_issue: str | None = None
        self._warned_maps: set = set()

    # ---------- 生命周期 ----------

    def running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def start(self) -> dict:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {"ok": True, "started": False, "reason": "already_running"}
            self._stop = threading.Event()
            self._emitted_key = None
            self._thread = threading.Thread(
                target=self._run, name="CapturePosition", daemon=True)
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
            last = dict(self._last_position) if self._last_position else None
            return {
                "running": bool(self._thread and self._thread.is_alive()),
                "connected": self._connected,
                "flow_decoding": self._flow_decoding,
                "source": CAPTURE_SOURCE_NAME,
                # 自动探测时 backend_url / capture_port 为 "" / None 表示还没锁定端口
                "backend_url": self._active_url,
                "capture_port": self._capture_port,
                "port_mode": "configured" if self._fixed_url else "scanned",
                "port_range": (str(self._capture_port) if self._fixed_url
                               else capture_port_range_label(self._port_start, self._port_end)),
                "health": self._health,
                "hint": self._hint,
                "detail": self._detail,
                "flow_id": self._flow_id,
                "flow_status": self._flow_status,
                "position_alive": self._position_alive,
                "last_position": last,
                "last_position_at": self._last_position_at,
                "age_seconds": age_seconds(last) if last else 0.0,
                "last_error": self._last_error,
            }

    # ---------- 内部：状态 ----------

    def _set_state(self, **kw) -> None:
        with self._lock:
            for key, value in kw.items():
                setattr(self, "_" + key, value)
            sig = (self._connected, self._flow_decoding, self._health, self._flow_id,
                   bool(self._last_position), self._hint, self._position_alive,
                   self._capture_port)
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

    def _emit_once(self, issue: str, msg: str) -> None:
        """同一原因只记一条日志；原因变了重新记。"""
        if issue == self._last_logged_issue:
            return
        self._last_logged_issue = issue
        self._emit(msg)

    def _wait(self) -> bool:
        """可被 ``stop()`` 打断的等待；返回 False 表示收到停止信号。"""
        return not self._stop.wait(self._poll_interval)

    # ---------- 内部：主循环 ----------

    def _run(self) -> None:
        if self._fixed_url:
            self._emit(f"[抓包位置] 位置源启动，轮询 {self._fixed_url}{STATE_PATH}")
        else:
            self._emit(f"[抓包位置] 位置源启动，自动探测端口 "
                       f"{capture_port_range_label(self._port_start, self._port_end)}")
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception as e:  # noqa: BLE001 — 线程里必须兜住，读失败要能自愈
                if self._stop.is_set():
                    break
                self._apply_backend_down(str(e))
            if not self._wait():
                break
        self._set_state(connected=False, health=HEALTH_BACKEND_DOWN, hint="已停止")
        self._emit("[抓包位置] 位置源已停止")

    def _poll_once(self) -> None:
        if self._active_url:
            state = self._fetch_state()
            if state is None:
                # 自动探测模式下丢掉当前端口，下一轮重新扫一遍（后端可能换了端口）
                if not self._fixed_url:
                    self._active_url = ""
                return  # _fetch_state 里已经把「连不上」写进状态
        else:
            state = self._scan_for_backend()
            if state is None:
                return  # 扫描里已经把「没找到」写进状态
        health = parse_health(state)
        raw = state.get("lastPosition")
        record = raw if isinstance(raw, dict) else None
        has_record = record is not None

        position = build_position(record, state) if record else None
        position_alive = False
        if position is not None:
            flow_id, flow_status, alive = flow_liveness(state, record)
            position["flowAlive"] = alive
            position_alive = alive
        else:
            flow_id, flow_status, alive = None, "", True

        flow_decoding = health["health"] == HEALTH_DECODING
        self._set_state(
            # ``connected`` 是「有可用的实时坐标」——界面上的圆点用它；``flow_decoding``
            # 才是契约里「真的接上了」那个判据（有可能刚握上手、还没有坐标）。
            connected=flow_decoding and position_alive,
            flow_decoding=flow_decoding,
            health=health["health"],
            detail=health["detail"],
            flow_id=health["flow_id"] if health["flow_id"] is not None else flow_id,
            flow_status=health["flow_status"] or flow_status,
            position_alive=position_alive,
            hint=self._hint_for(health, position, alive, has_record),
            last_error=None,
        )
        if position is not None:
            self._set_state(last_position=position, last_position_at=time.time())
        self._log_health_once(health, position, alive)
        self._maybe_emit_position(position, alive)

    def _fetch_state(self) -> dict | None:
        url = self._active_url + STATE_PATH
        try:
            with request.urlopen(url, timeout=self._http_timeout) as response:
                payload = json.loads(response.read().decode("utf-8", errors="ignore"))
        except (error.URLError, TimeoutError, OSError, ValueError) as exc:
            self._apply_backend_down(f"{type(exc).__name__}: {exc}")
            return None
        if not isinstance(payload, dict):
            self._apply_backend_down("后端返回的不是状态对象")
            return None
        self._last_logged_issue = None  # 连上了，下次出问题重新提示
        return payload

    def _scan_for_backend(self) -> dict | None:
        """按 ``8765, 8766, … 8775`` 升序探端口，锁定第一个答得像抓包后端的，并返回它的状态。

        扫描本身是只读的（就是 ``GET /api/state``），不会去启动 / 重启后端。没找到时
        有冷却，避免在没有后端的期间每轮都打 10 个端口。
        """
        now = time.time()
        if now - self._last_scan_at < self._scan_cooldown:
            self._apply_not_found()  # 冷却期内保持「没找到」状态，不重复探测
            return None
        self._last_scan_at = now
        for url in self._candidates:
            payload = probe_capture_state(url, self._scan_timeout)
            if payload is None:
                continue
            self._set_state(active_url=url, capture_port=url_port(url),
                            last_port=url_port(url), last_error=None)
            self._last_logged_issue = None
            self._emit(f"[抓包位置] 在 {url} 找到抓包后端")
            return payload
        self._apply_not_found()
        return None

    def _apply_backend_down(self, reason: str) -> None:
        target = self._active_url or f"端口 {capture_port_range_label(self._port_start, self._port_end)}"
        hint = f"连接不到抓包后端 {target}"
        self._set_state(connected=False, health=HEALTH_BACKEND_DOWN,
                        hint=hint, detail=reason, last_error=reason)
        self._emit_once(f"down:{reason}", f"[抓包位置] {hint}（{reason}）")

    def _apply_not_found(self) -> None:
        port_range = capture_port_range_label(self._port_start, self._port_end)
        if self._last_port is not None:
            # 锁过端口又掉了：说清是哪个口掉了，并说明正在重新探测
            hint = f"连接不到抓包后端 127.0.0.1:{self._last_port}（正在重新探测 {port_range}）"
        else:
            hint = f"没有找到抓包后端（已探测 {port_range}）"
        self._set_state(connected=False, health=HEALTH_BACKEND_DOWN, hint=hint,
                        detail=hint, last_error=hint, active_url="", capture_port=None)
        self._emit_once(f"notfound:{port_range}",
                        f"[抓包位置] {hint}；后端必须先于游戏主连接启动，"
                        "起晚了就只能重开游戏，重启后端没有用")

    @staticmethod
    def _hint_for(health: dict, position: dict | None, alive: bool, has_record: bool) -> str:
        # 坐标是死的（所属连接已结束）比「连接状态」更该先说明：地图上画的最后一格
        # 会一直停在那儿，不说清就会被当成实时位置。
        if position is not None and not alive:
            return "最新坐标来自已经结束的连接，先不画到地图上"
        state = health.get("health")
        if state == HEALTH_DECODING:
            if position is None:
                return "已接上，等待第一条坐标（玩家动一下）" if not has_record else "坐标字段无法解析"
            return ""
        if state == HEALTH_NO_CONNECTION:
            detail = str(health.get("detail") or "")
            if detail.startswith("后端还没看到"):
                return "后端还没看到游戏连接（后端要先于游戏启动）"
            return "没有仍在继续的连接（游戏退出了，或后端被重启过）"
        return {
            HEALTH_HANDSHAKE_PENDING: "已发现连接，等待登录握手完成",
            HEALTH_MISSED_HANDSHAKE:
                "本局拿不到连接起点：请重开游戏（重启后端没用，后端须先于游戏启动）",
        }.get(state, health.get("detail") or "位置源状态未知")

    def _log_health_once(self, health: dict, position: dict | None, alive: bool) -> None:
        state = health.get("health")
        if state == HEALTH_DECODING:
            if position is not None and not alive:
                self._emit_once("position_dead_flow",
                                "[抓包位置] 最新坐标来自已经结束的连接，先不画到地图上")
            return
        if state == HEALTH_BACKEND_DOWN:
            return  # _apply_backend_down 已经记过
        self._emit_once(
            f"health:{state}:{health.get('flow_id')}",
            f"[抓包位置] {self._hint_for(health, position, alive, False)}",
        )

    def _maybe_emit_position(self, position: dict | None, alive: bool) -> None:
        if position is None or not alive:
            return
        key = position_key(position)
        if key is not None and key == self._emitted_key:
            return
        self._emitted_key = key
        self._maybe_log_map(position)
        try:
            self._on_position(position)
        except Exception as e:  # noqa: BLE001 — 回调失败不能拖垮轮询线程
            self._emit(f"[抓包位置] 位置回调异常: {e}")

    def _maybe_log_map(self, position: dict) -> None:
        """地图认不出来时提示一次（同一场景只提示一次）。"""
        if str(position.get("mapId") or "").strip():
            return
        scene = position.get("scene")
        if scene in self._warned_maps:
            return
        self._warned_maps.add(scene)
        if position.get("mapIdSource") == "no_parent_map":
            self._emit(f"[抓包位置] 场景 {scene}（{position.get('levelName') or position.get('levelId')}）"
                       "不属于任何大地图，没有对应网格 —— 这是预期行为")
        else:
            self._emit(f"[抓包位置] 场景 {scene!r} 不在场景表里，也没有地图 WS 对照，"
                       "地图名暂时定不出来")


__all__ = [
    "CAPTURE_SOURCE_NAME",
    "DEFAULT_CAPTURE_PORT",
    "CAPTURE_PORT_END",
    "CAPTURE_PORT_START",
    "FLOW_IDLE_SECONDS",
    "HEALTH_BACKEND_DOWN",
    "HEALTH_DECODING",
    "HEALTH_HANDSHAKE_PENDING",
    "HEALTH_MISSED_HANDSHAKE",
    "HEALTH_NO_CONNECTION",
    "STATE_PATH",
    "CapturePositionClient",
    "age_seconds",
    "as_float",
    "build_position",
    "capture_port_candidates",
    "capture_port_range_label",
    "default_capture_url",
    "flow_liveness",
    "flow_status_is_missed_handshake",
    "flow_status_is_terminal",
    "looks_like_capture_state",
    "parse_flow_id",
    "parse_health",
    "position_key",
    "probe_capture_state",
    "resolve_map_id",
    "url_port",
]
