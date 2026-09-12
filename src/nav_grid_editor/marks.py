#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""官方地图标记抓取（公开口径 / 认证口径）。

两条口径：

- **公开**：三个端点都无需鉴权，只拿得到 `marks`（地图上固定的采集物、怪种等）。
- **认证**：`saveMarks` 里才有**玩家自建的结构**（滑索、暗管、供电桩、中继器…），
  而 `saveMarks` 只在带账号凭证的请求里才有值。认证链路：
  content → HG grant → oauth code → dId → cred/token → 带签名请求。

本模块是**唯一实现**：命令行（``scripts/fetch_endfield_map_marks*.py``）和网页
（``POST /api/marks/fetch``）都调它，免得两边各写一遍再慢慢漂移。

凭证：认证口径需要 `hg/check` 响应里的 ``data.content``。按用户要求，它**只从
本机文件读**（``configs/hg_content.txt``，已 gitignore），不进浏览器、不进日志、
不进命令行参数——见 :func:`read_hg_content`。
"""

from __future__ import annotations

import getpass
import hashlib
import hmac
import json
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable
from urllib import error, parse, request

from .map_device_id import CONFIG_DIR, ensure_map_device_id

API_HOST = "https://zonai.skland.com"
HG_GRANT_URL = "https://as.hypergryph.com/user/oauth2/v2/grant"
HG_APP_CODE = "4ca99fa6b56cc2ba"
ORIGIN = "https://game.skland.com"
REFERER = "https://game.skland.com/map/endfield"
UA = "Mozilla/5.0 ok-ef map dump script"

#: 公开脚本会排除这两项（场景装饰，不是可拾取物品）；认证口径默认**不排除**，
#: 因为滑索架正是要抓的东西
SLACKLINE_MARKS = {"长距滑索架", "滑索架"}

#: structures.json 只收这个主类下的子类（滑索/暗管/供电设备…）
STRUCTURE_MAIN_TYPE = "工业设施"

#: 未鉴权的三个端点
PUBLIC_BASE = API_HOST
TIMEOUT = 30

Log = Callable[[str], None]


def _noop(_msg: str) -> None:
    pass


# =========================================================
# 输出位置
# =========================================================

def marks_dir(assets_root: Path) -> Path:
    """公开口径产物目录：<assets>/items/map"""
    return Path(assets_root) / "items" / "map"


def marks_auth_dir(assets_root: Path) -> Path:
    """认证口径产物目录：<assets>/items/map_auth。

    **刻意与公开口径分开**：认证口径默认不排除滑索架，写进公开目录会破坏
    两个仓库约定的格式一致。
    """
    return Path(assets_root) / "items" / "map_auth"


def icons_dir(assets_root: Path) -> Path:
    """标记图标缓存：<assets>/items/icons/<templateId>.<ext>。

    图标来自 catalog/markTemplates 里的 `pic`（远在 bbs.hycdn.cn）。抓到本地一份，
    前端画地图时就不用每次打远程 CDN（也不依赖外网）。
    """
    return Path(assets_root) / "items" / "icons"


#: 文件名里不能出现的字符（Windows 最严：\ / : * ? " < > | 加控制字符）
_ICON_BAD_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def icon_filename(name: str) -> str:
    """物品名 → 安全的文件名。中文保留，只清掉文件系统不认的字符。

    Windows 还不接受结尾的点和空格（会被静默吃掉，导致名字对不上）。
    """
    s = _ICON_BAD_CHARS.sub("_", (name or "").strip()).strip(" .")
    return s or "unnamed"


def download_icons(entries: list[tuple[str, str, str]], icons_dir_path: Path,
                   log: Log = _noop) -> dict[str, str]:
    """下载图标，**按中文名命名并去重**；返回 ``{templateId: 文件名}``。

    entries 是 ``(templateId, 物品名, pic_url)``。去重口径只看**名字**：
    实测 204 个 templateId 只有 168 个不同的名字，所以同名合并（12 组，如
    「供电设备」5 个 id → 一个文件）后是 168 个文件 / 168 次请求。

    同图不同名的（3 组）各下一份——**不做"同 URL 只下一次"的额外优化**，
    按用户要求保持逻辑简单。
    """
    icons_dir_path = Path(icons_dir_path)
    icons_dir_path.mkdir(parents=True, exist_ok=True)

    # 先按名字归并（同名多 id → 一个文件）
    by_name: dict[str, dict] = {}
    for tid, name, url in entries:
        if not name or not url:
            continue
        e = by_name.setdefault(name, {"url": url, "tids": []})
        e["tids"].append(tid)
        e["url"] = url

    tid_file: dict[str, str] = {}
    used: set[str] = set()
    stats = {"network": 0, "failed": 0, "written": 0, "skipped": 0}

    for name in sorted(by_name):
        e = by_name[name]
        url = e["url"]
        ext = Path(url.split("?", 1)[0]).suffix or ".png"
        base = icon_filename(name)
        fname = base + ext
        # 清洗后可能撞名（"A/B" 与 "A:B" 都变 A_B），加序号区分
        if fname in used:
            i = 2
            while f"{base}_{i}{ext}" in used:
                i += 1
            fname = f"{base}_{i}{ext}"
        used.add(fname)
        dest = icons_dir_path / fname

        if dest.is_file() and dest.stat().st_size > 0:
            stats["skipped"] += 1
        else:
            try:
                req = request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                    "Referer": REFERER})
                with request.urlopen(req, timeout=TIMEOUT) as resp:
                    data = resp.read()
                stats["network"] += 1
            except Exception as ex:  # noqa: BLE001
                stats["failed"] += 1
                log(f"图标下载失败 {name}: {ex}")
                continue
            try:
                tmp = dest.with_name(dest.name + ".part")
                tmp.write_bytes(data)
                tmp.replace(dest)
                stats["written"] += 1
            except OSError as ex:
                stats["failed"] += 1
                log(f"图标写盘失败 {fname}: {ex}")
                continue
        for tid in e["tids"]:
            tid_file[tid] = fname

    log(f"图标：新下 {stats['network']} 个（已有 {stats['skipped']}）→ "
        f"覆盖 {len(tid_file)} 个 templateId，失败 {stats['failed']}")
    return tid_file


def hg_content_path(config_dir: Path | None = None) -> Path:
    return Path(config_dir or CONFIG_DIR) / "hg_content.txt"


def read_hg_content(config_dir: Path | None = None) -> str:
    """从本机文件读 ``content`` 凭证；没有则返回空串。

    只在本地文件里找——凭证不进浏览器、不进日志、不进 argv。
    文件里可以带引号/换行，会自动去掉。
    """
    p = hg_content_path(config_dir)
    try:
        return p.read_text(encoding="utf-8").strip().strip('"').strip()
    except OSError:
        return ""


def write_hg_content(content: str, config_dir: Path | None = None) -> Path:
    """把凭证写到本地文件（网页不调这个，留给用户/命令行用）。"""
    p = hg_content_path(config_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content.strip() + "\n", encoding="utf-8")
    return p


# =========================================================
# HTTP 小工具
# =========================================================

def _open_json(req: request.Request):
    try:
        with request.urlopen(req, timeout=TIMEOUT) as resp:
            text = resp.read().decode("utf-8", errors="ignore")
    except error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")[:1000]
        raise RuntimeError(f"HTTP {e.code} {e.reason}: {body}") from e
    return json.loads(text) if text else None


def _get_json(path: str) -> Any:
    """匿名 GET（公开口径用）。"""
    req = request.Request(API_HOST + path, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": REFERER,
        "Origin": ORIGIN,
    })
    return _open_json(req)


def _post_json(url: str, payload: dict, headers: dict | None = None) -> Any:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    h = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": ORIGIN,
        "Referer": REFERER,
    }
    if headers:
        h.update(headers)
    return _open_json(request.Request(url, data=body, headers=h, method="POST"))


# =========================================================
# 聚合
# =========================================================

def _freeze(groups: dict) -> dict:
    """{mapId: {名: {(x,y,z): 点}}} → 排序后的 JSON 结构。

    点位按 (x,y,z) 排序：接口每次返回的顺序并不稳定，不排的话同一批数据重跑
    会产生大段"看着变了、其实没变"的 diff。
    """
    def _sorted_points(v: dict) -> list:
        return sorted(v.values(), key=lambda p: (p["x"], p["y"], p["z"]))

    return {
        m: {k: _sorted_points(v) for k, v in sorted(g.items())}
        for m, g in sorted(groups.items())
    }


def merge_icon_sources(catalog_struct: dict, entries: list) -> list:
    """合并两个图标来源，返回 ``[(tid, 物品名, url)]``。

    实测**必须两个都用**：
    - catalog 的子类 pic 覆盖全部 204 个 templateId（168 个子类 100% 带图）
    - markTemplates 的 pic 更精确，但**只有在地图上有公开点位的模板才有**——
      滑索/暗管/供电设备这些玩家自建结构不在匿名 markTemplates 里，只靠它会有
      28 个模板拿不到图标（表现就是地图上只剩一个小黄点）
    - 名字优先用 markTemplates 的（更具体：「供电终端」而不是子类「供电设备」），
      没有才退回子类名
    """
    src: dict = {}
    for tid, info in (catalog_struct or {}).items():
        if info.get("pic"):
            src[tid] = (info.get("name") or "", info["pic"])
    for tid, name, url in entries:
        base = src.get(tid, ("", ""))
        src[tid] = (name or base[0], url or base[1])
    return [(tid, n, u) for tid, (n, u) in src.items() if n and u]


def _freeze_points(points: dict) -> dict:
    """{mapId: [{t,x,y,z}]} 按坐标排序（同 _freeze，保证输出确定）。"""
    return {m: sorted(v, key=lambda p: (p["x"], p["y"], p["z"]))
            for m, v in sorted(points.items())}


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# =========================================================
# 公开口径
# =========================================================

def fetch_public(out_dir: Path, log: Log = _noop) -> dict:
    """抓公开标记，写 summary.json / item_names.json / template_catalog.json。

    只按 mapId 查：实测 mapId 一次就覆盖该地图所有 level，逐 level 再查是纯冗余。
    """
    out_dir = Path(out_dir)
    log("GET /map/tree")
    tree = _get_json("/web/v1/game/endfield/map/tree")
    requests_made = 1

    all_maps: dict = defaultdict(lambda: defaultdict(dict))
    points: dict = defaultdict(list)
    icon_entries: list = []
    names: set = set()
    tid_name: dict = {}
    dupes = 0

    for game_map in (tree.get("data") or {}).get("maps") or []:
        map_id = game_map.get("id")
        if not map_id:
            continue
        path = "/web/v1/game/endfield/map/mark/list?" + parse.urlencode({"mapId": map_id})
        log(f"GET {path}")
        data = (_get_json(path) or {}).get("data") or {}
        requests_made += 1

        templates = {t["id"]: t for t in data.get("markTemplates") or []}
        for tid, t in templates.items():
            n = (t.get("name") or "").strip()
            if n:
                tid_name.setdefault(tid, n)
            pic = (t.get("pic") or "").strip()
            if pic and n:
                icon_entries.append((tid, n, pic))
        tmap = {tid: (t.get("name") or "").strip() for tid, t in templates.items()}
        names.update(n for n in tmap.values() if n and n not in SLACKLINE_MARKS)

        def add(mark: dict, _tmap=tmap, _map_id=map_id) -> None:
            nonlocal dupes
            tid = mark.get("templateId")
            name = _tmap.get(tid)
            if not name or name in SLACKLINE_MARKS:
                return
            pos = mark.get("pos")
            if not isinstance(pos, dict):
                return
            x, y, z = pos.get("x"), pos.get("y"), pos.get("z")
            if None in (x, y, z):
                return
            mid = mark.get("mapId") or _map_id
            bucket = all_maps[mid][name]
            if (x, y, z) in bucket:
                dupes += 1
            bucket[(x, y, z)] = {"x": x, "y": y, "z": z}
            # 另存一份带 templateId 的点位：summary.json 按物品名存，丢了
            # templateId，前端就没法据此找图标
            points[mid].append({"t": tid, "x": x, "y": y, "z": z})

        for mark in data.get("marks") or []:
            add(mark)
        for mark in data.get("saveMarks") or []:
            add(mark)

    log("GET /map/catalog")
    catalog = _get_json("/web/v1/game/endfield/map/catalog") or {}
    requests_made += 1
    struct = _parse_catalog(catalog)
    for tid, item in struct.items():
        if tid in tid_name:
            item["name"] = tid_name[tid]

    summary = _freeze(all_maps)
    _write_json(out_dir / "summary.json", summary)
    _write_json(out_dir / "item_names.json", sorted(names))
    _write_json(out_dir / "points.json", _freeze_points(points))

    log("下载图标…")
    tid_file = download_icons(merge_icon_sources(struct, icon_entries),
                              icons_dir(out_dir.parent.parent), log)
    for tid, item in struct.items():
        item["icon"] = tid_file.get(tid, "")
    _write_json(out_dir / "template_catalog.json", struct)

    return {
        "kind": "public",
        "out_dir": str(out_dir),
        "requests": requests_made,
        "maps": len(summary),
        "items": len(names),
        "points": sum(len(v) for g in summary.values() for v in g.values()),
        "duplicates": dupes,
        "icons": len(tid_file),
        "files": ["summary.json", "item_names.json", "template_catalog.json", "points.json"],
    }


# =========================================================
# 认证口径
# =========================================================

def _request_hg_grant_code(content: str) -> str:
    hg_token = parse.unquote(content.strip().strip('"'))
    resp = _post_json(HG_GRANT_URL, {"token": hg_token, "appCode": HG_APP_CODE, "type": 0})
    if not isinstance(resp, dict) or resp.get("status") != 0:
        raise RuntimeError(f"HG grant 失败: {resp}")
    code = ((resp.get("data") or {}).get("code") or "").strip()
    if not code:
        raise RuntimeError("HG grant 没有返回 oauth code")
    return code


def _request_map_cred(oauth_code: str, device_id: str) -> dict:
    resp = _post_json(
        f"{API_HOST}/web/v1/user/auth/generate_cred_by_code",
        {"kind": 1, "code": oauth_code},
        headers={"platform": "3", "vName": "1.0.0",
                 "timestamp": str(int(time.time())), "dId": device_id},
    )
    if not isinstance(resp, dict) or resp.get("code") != 0:
        hint = ""
        if isinstance(resp, dict) and resp.get("code") == 10001:
            hint = (f"（设备信息无效：{hg_content_path().parent / 'map_device_id.json'} 里的 dId "
                    "可能被风控拒绝，删掉它再跑一次即可重新铸造）")
        raise RuntimeError(f"generate_cred_by_code 失败: {resp}{hint}")
    data = resp.get("data") or {}
    if not str(data.get("cred") or "").strip() or not str(data.get("token") or "").strip():
        raise RuntimeError("generate_cred_by_code 返回里缺少 cred/token")
    return resp


def exchange_content(content: str, log: Log = _noop) -> dict:
    """content → oauth code → dId → cred/token。"""
    log("换取 oauth code…")
    oauth_code = _request_hg_grant_code(content)
    log("取得设备ID(dId)…")
    device_id = ensure_map_device_id()
    if not device_id:
        raise RuntimeError("设备ID(dId)不可用：自动铸造失败（需装 Edge/Chrome 且能访问 fp-it.portal101.cn）")
    log("换取 cred/token…")
    resp = _request_map_cred(oauth_code, device_id)
    data = resp.get("data") or {}
    return {
        "cred": str(data.get("cred") or "").strip(),
        "sign_token": str(data.get("token") or "").strip(),
        "d_id": device_id,
        "sign_time": {"clientTime": str(int(time.time())),
                      "serverTime": str(resp.get("timestamp") or int(time.time()))},
    }


class SignedClient:
    """带签名头的请求客户端（platform/vName/timestamp/dId + cred + sign）。"""

    def __init__(self, auth: dict):
        self.cred = str(auth.get("cred") or "")
        self.sign_token = str(auth.get("sign_token") or "")
        self.sign_time = auth.get("sign_time") if isinstance(auth.get("sign_time"), dict) else {}
        self.device_id = str(auth.get("d_id") or "")

    def adjusted_timestamp(self) -> str:
        now = int(time.time())
        try:
            client_time = int(self.sign_time.get("clientTime") or 0)
            server_time = int(self.sign_time.get("serverTime") or 0)
        except (TypeError, ValueError):
            client_time = server_time = 0
        return str(server_time + (now - client_time)) if client_time and server_time else str(now)

    def sign_headers(self, url: str, method: str = "GET", body: str = "") -> dict:
        h = {"platform": "3", "vName": "1.0.0",
             "timestamp": self.adjusted_timestamp(), "dId": self.device_id}
        parsed = parse.urlsplit(url)
        payload = parsed.path
        payload += parsed.query if method.upper() == "GET" else body
        payload += h["timestamp"]
        payload += json.dumps(
            {"platform": h["platform"], "timestamp": h["timestamp"],
             "dId": h["dId"], "vName": h["vName"]},
            separators=(",", ":"), ensure_ascii=False)
        digest = hmac.new(self.sign_token.encode("utf-8"), payload.encode("utf-8"),
                          hashlib.sha256).hexdigest()
        h["sign"] = hashlib.md5(digest.encode("utf-8")).hexdigest()
        return h

    def get(self, path: str, params: dict | None = None):
        query = f"?{parse.urlencode(params)}" if params else ""
        url = f"{API_HOST}{path}{query}"
        headers = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
                   "Content-Type": "application/json", "Origin": ORIGIN,
                   "Referer": REFERER, "cred": self.cred}
        headers.update(self.sign_headers(url, "GET"))
        return _open_json(request.Request(url, headers=headers, method="GET"))


def collect_roles(binding_resp: Any) -> list[dict]:
    """从 binding 里取 (roleId, serverId)；终末地角色在 gameMap.endfield 下。"""
    data = binding_resp.get("data") if isinstance(binding_resp, dict) else {}
    game_map = data.get("gameMap") if isinstance(data, dict) else {}
    endfield = game_map.get("endfield") if isinstance(game_map, dict) else None
    if not isinstance(endfield, dict):
        for entry in data.get("list") or []:
            if isinstance(entry, dict) and entry.get("appCode") == "endfield":
                endfield = entry
                break
    if not isinstance(endfield, dict):
        return []
    out: list[dict] = []
    for binding in endfield.get("bindingList") or []:
        if not isinstance(binding, dict):
            continue
        roles = []
        if isinstance(binding.get("defaultRole"), dict):
            roles.append(binding["defaultRole"])
        roles.extend(r for r in binding.get("roles") or [] if isinstance(r, dict))
        for role in roles:
            rid, sid = role.get("roleId"), role.get("serverId")
            if rid is None or sid is None:
                continue
            item = {"roleId": str(rid), "serverId": str(sid)}
            if item not in out:
                out.append(item)
    return out


def _collect_level_queries(tree_resp: Any) -> list[dict]:
    out = []
    for gm in ((tree_resp or {}).get("data") or {}).get("maps") or []:
        if not isinstance(gm, dict):
            continue
        mid = str(gm.get("id") or "").strip()
        if not mid:
            continue
        for lv in gm.get("levels") or []:
            if not isinstance(lv, dict) or lv.get("type") != 1:
                continue
            lid = str(lv.get("id") or "").strip()
            if lid:
                out.append({"mapId": mid, "levelId": lid})
    return out


def _parse_catalog(catalog_resp: Any) -> dict:
    """catalog 响应 → ``{templateId: {name, mainType, subType, pic}}``。

    公开与认证两条口径都用它，免得各自解析一遍。
    """
    out: dict = {}
    for mt in ((catalog_resp or {}).get("data") or {}).get("mainTypes") or []:
        if not isinstance(mt, dict):
            continue
        for st in mt.get("subTypes") or []:
            for tid in st.get("templateIds") or []:
                out[tid] = {
                    "name": st.get("name"),
                    "mainType": mt.get("name"),
                    "subType": st.get("name"),
                    "pic": st.get("pic"),
                }
    return out


def _collect_structure_types(catalog_resp: Any) -> dict:
    out = {}
    for mt in ((catalog_resp or {}).get("data") or {}).get("mainTypes") or []:
        if not isinstance(mt, dict) or mt.get("name") != STRUCTURE_MAIN_TYPE:
            continue
        for st in mt.get("subTypes") or []:
            for tid in st.get("templateIds") or []:
                out[tid] = str(st.get("name") or "")
    return out


def fetch_auth(out_dir: Path, content: str, log: Log = _noop,
               per_level: bool = False, exclude_slacklines: bool = False,
               raw_dir: Path | None = None) -> dict:
    """抓认证标记（含玩家自建的滑索/暗管），写 summary/item_names/structures.json。

    默认**不排除**任何标记；``exclude_slacklines=True`` 恢复公开口径的排除。
    """
    out_dir = Path(out_dir)
    exclude = SLACKLINE_MARKS if exclude_slacklines else set()

    auth = exchange_content(content, log)
    client = SignedClient(auth)

    log("读取地图树 / 分类 / 角色…")
    tree = client.get("/web/v1/game/endfield/map/tree")
    catalog = client.get("/web/v1/game/endfield/map/catalog")
    binding = client.get("/api/v1/game/player/binding")

    roles = collect_roles(binding)
    if not roles:
        raise RuntimeError("binding 里没有终末地角色（roleId/serverId），无法查 saveMarks")
    map_ids = [str(m.get("id") or "").strip()
               for m in ((tree or {}).get("data") or {}).get("maps") or [] if isinstance(m, dict)]
    map_ids = [m for m in map_ids if m]
    if not map_ids:
        raise RuntimeError("map/tree 里没有地图")
    struct_types = _collect_structure_types(catalog)

    queries = [{"mapId": m} for m in map_ids]
    if per_level:
        queries.extend(_collect_level_queries(tree))

    summary: dict = defaultdict(lambda: defaultdict(dict))
    structures: dict = defaultdict(lambda: defaultdict(dict))
    points: dict = defaultdict(list)
    icon_entries: list = []
    tid_name: dict = {}
    names: set = set()
    dupes = saved_marks = requests_made = 0

    for role in roles:
        for q in queries:
            params = dict(q, roleId=role["roleId"], serverId=role["serverId"])
            resp = client.get("/web/v1/game/endfield/map/mark/list", params)
            requests_made += 1
            data = (resp or {}).get("data") or {}
            tmap = {t["id"]: (t.get("name") or "").strip() for t in data.get("markTemplates") or []}
            names.update(n for n in tmap.values() if n and n not in exclude)
            for t in data.get("markTemplates") or []:
                pic = (t.get("pic") or "").strip()
                nm = (t.get("name") or "").strip()
                if nm:
                    tid_name.setdefault(t["id"], nm)
                if pic and nm:
                    icon_entries.append((t["id"], nm, pic))

            def add(mark: dict, _tmap=tmap, _mid=q["mapId"]) -> None:
                nonlocal dupes
                tid = mark.get("templateId")
                name = _tmap.get(tid)
                if not name or name in exclude:
                    return
                pos = mark.get("pos")
                if not isinstance(pos, dict):
                    return
                x, y, z = pos.get("x"), pos.get("y"), pos.get("z")
                if None in (x, y, z):
                    return
                mid = mark.get("mapId") or _mid
                coord = (x, y, z)
                bucket = summary[mid][name]
                if coord in bucket:
                    dupes += 1
                bucket[coord] = {"x": x, "y": y, "z": z}
                points[mid].append({"t": tid, "x": x, "y": y, "z": z})
                sub = struct_types.get(tid)
                if sub:
                    structures[mid][sub].setdefault(
                        coord, {"name": name, "x": x, "y": y, "z": z})

            for mark in data.get("marks") or []:
                add(mark)
            for mark in data.get("saveMarks") or []:
                saved_marks += 1
                add(mark)

            log(f"{q['mapId']:8} server={role['serverId']:>4} "
                f"marks={len(data.get('marks') or [])} "
                f"saveMarks={len(data.get('saveMarks') or [])}")
            if raw_dir is not None:
                _write_json(Path(raw_dir) / f"mark_list__map_{q['mapId']}"
                                          f"__server_{role['serverId']}.json", resp)

    summary_f = _freeze(summary)
    structures_f = _freeze(structures)
    _write_json(out_dir / "summary.json", summary_f)
    _write_json(out_dir / "item_names.json", sorted(names))
    _write_json(out_dir / "structures.json", structures_f)
    _write_json(out_dir / "points.json", _freeze_points(points))

    log("下载图标…")
    auth_catalog = _parse_catalog(catalog)
    tid_file = download_icons(merge_icon_sources(auth_catalog, icon_entries),
                              icons_dir(out_dir.parent.parent), log)
    # 认证口径也出一份 catalog：前端画图标要知道每个 templateId 的类别与图标文件名
    for tid, item in auth_catalog.items():
        if tid in tid_name:
            item["name"] = tid_name[tid]
        item["icon"] = tid_file.get(tid, "")
    _write_json(out_dir / "template_catalog.json", auth_catalog)

    return {
        "kind": "auth",
        "out_dir": str(out_dir),
        "roles": len(roles),
        "requests": requests_made,
        "maps": len(summary_f),
        "items": len(names),
        "points": sum(len(v) for g in summary_f.values() for v in g.values()),
        "saved_marks": saved_marks,
        "duplicates": dupes,
        "icons": len(tid_file),
        "structures": {m: {s: len(p) for s, p in sorted(g.items())}
                       for m, g in sorted(structures_f.items())},
        "structure_points": sum(len(v) for g in structures_f.values() for v in g.values()),
        "excluded": bool(exclude_slacklines),
        "files": ["summary.json", "item_names.json", "structures.json", "points.json",
                  "template_catalog.json"],
    }


# =========================================================
# 校验（阈值照抄 ok-end-field 的 CI，防止静默产出缩水的数据）
# =========================================================

MIN_MAPS = 3
MIN_ITEMS = 20
MIN_POINTS = 100
MAX_DROP = 0.20


def _count_points(d: dict) -> int:
    return sum(len(v) for g in d.values() for v in g.values())


def validate(stats: dict, summary: dict, old_summary: dict | None,
             old_names: set | None = None) -> list[str]:
    """返回错误列表（空 = 通过）。相对旧数据三项跌幅都不许超过 20%。"""
    errs: list[str] = []
    if stats.get("maps", 0) < MIN_MAPS:
        errs.append(f"地图数 {stats.get('maps')} < {MIN_MAPS}")
    if stats.get("items", 0) < MIN_ITEMS:
        errs.append(f"物品名 {stats.get('items')} < {MIN_ITEMS}")
    if stats.get("points", 0) < MIN_POINTS:
        errs.append(f"点位总数 {stats.get('points')} < {MIN_POINTS}")
    for m, g in summary.items():
        if not g:
            errs.append(f"{m} 一个点位都没有")
    if old_summary:
        for label, new_v, old_v in (
            ("地图数", stats.get("maps", 0), len(old_summary)),
            ("物品名", stats.get("items", 0), len(old_names or {n for g in old_summary.values() for n in g})),
            ("点位总数", stats.get("points", 0), _count_points(old_summary)),
        ):
            if old_v and new_v < old_v * (1 - MAX_DROP):
                errs.append(f"{label} {old_v} -> {new_v}，跌幅超过 {MAX_DROP:.0%}")
    return errs


def load_map_markers(assets_root: Path, map_id: str) -> dict:
    """给前端画地图用：某张图的全部点位 + templateId 元信息。

    合并**公开与认证**两份 points.json——认证那份才含玩家自建结构。两份会大量
    重叠（认证口径是公开口径的超集），所以按 (templateId, x, y, z) 去重。
    """
    assets_root = Path(assets_root)
    templates: dict = {}
    seen: set = set()
    pts: list = []
    for d in (marks_dir(assets_root), marks_auth_dir(assets_root)):
        catalog = load_json(d / "template_catalog.json")
        if isinstance(catalog, dict):
            for tid, info in catalog.items():
                # **认证口径放后面并覆盖**：它的名字来自认证 markTemplates 里的具体
                # 物品名（「供电桩」），而公开口径只能拿到 catalog 的子类名，那是个
                # **类别**（「供电设备」）。实测 13 个 templateId 两边不一致
                # （供电设备/供电桩、滑索/长距滑索架、暗管/暗管入口…），
                # 用错就会把供电桩画成供电设备的图标。
                templates[tid] = {
                    "n": info.get("name"),
                    "m": info.get("mainType"),
                    "s": info.get("subType"),
                    # 图标文件名（中文名.png）；空串表示没有图标，前端画圆点
                    "icon": info.get("icon") or "",
                }
        data = load_json(d / "points.json")
        if isinstance(data, dict):
            for p in data.get(map_id) or []:
                key = (p.get("t"), p.get("x"), p.get("y"), p.get("z"))
                if key in seen:
                    continue
                seen.add(key)
                pts.append([p.get("t"), p.get("x"), p.get("y"), p.get("z")])

    pts.sort(key=lambda r: (r[1], r[2], r[3]))
    return {"map": map_id, "templates": templates, "points": pts}


def find_icon(assets_root: Path, filename: str) -> Path | None:
    """按文件名取本地图标。只接受**裸文件名**——任何目录成分都拒掉。"""
    name = (filename or "").strip()
    if not name or name != Path(name).name or "/" in name or "\\" in name:
        return None
    p = icons_dir(assets_root) / name
    return p if p.is_file() else None


def load_json(path: Path):
    """读一个可能不存在的 JSON；坏了/没有都返回 None（不该拦住抓取）。"""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# =========================================================
# 命令行共用的小工具
# =========================================================

def prompt_content() -> str:
    """交互式读取 content（命令行用；网页走本地文件，不走这里）。"""
    return getpass.getpass("hg/check data.content: ").strip()


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)
