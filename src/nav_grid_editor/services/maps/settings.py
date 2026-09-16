# -*- coding: utf-8 -*-
"""地图服务的运行配置、路径默认值、格式常量和输入校验。"""

from __future__ import annotations

import os
import re
from pathlib import Path

try:
    from PIL import Image  # type: ignore

    Image.MAX_IMAGE_PIXELS = None
    try:
        from PIL import features as _pil_features

        WEBP_OK = bool(_pil_features.check("webp"))
    except Exception:  # pragma: no cover
        WEBP_OK = False
except Exception:  # pragma: no cover
    Image = None
    WEBP_OK = False

PLAYWRIGHT_OK = False
try:
    import playwright  # noqa: F401

    PLAYWRIGHT_OK = True
except Exception:  # pragma: no cover
    PLAYWRIGHT_OK = False


MAP_URL = "https://game.skland.com/map/endfield"

# ---- 2D 网格稠密 npz 的格式常量 ----
# 必须与 ok-end-field/src/nav/grid_io.py 逐字一致（跨仓库不能 import，只能字面量对齐）：
# 读方会校验 magic/schema_version/axis_convention，值域只接受下面这三种状态。
GRID_MAGIC = "okef-nav-grid"
GRID_SCHEMA_VERSION = 2
GRID_SUFFIX = ".grid.npz"
GRID_AXIS_CONVENTION = (
    "world = origin + (i,j)*cell_size; i=row=z, j=col=x, both positive; "
    "origin is the min corner of cells[0,0]; 2D ignores y"
)
CELL_UNKNOWN = 0
CELL_FREE = 1
CELL_BLOCKED = 2
#: 稠密数组元素数上限。格子放得太散时 (max-min)² 会直接吃光内存，所以要有上限；
#: 但它必须容得下**整张地图**，否则永远存不下来。实测 map02@4 的标记范围是
#: x -1750..491 × z -2037..1550，cell_size=1 时约 9.6M 格，旧的 4M 上限连半张图
#: 都盖不住（用户就是撞在这上面）。uint8 一格一字节，64M = 64MB，本地工具可接受，
#: 同时仍能挡住写错世界坐标导致的离谱范围（偏出约 8000 格才会触发）。
DENSE_CELL_LIMIT = 64_000_000
#: 编辑器可保存的已涂格子上限（与前端框选上限一致）
EDIT_CELL_LIMIT = 300_000
#: 与 ok-end-field 的 grid_io.load_grid / scripts/nav/verify_grid.py 对齐的硬性约束
GRID_MEMBERS = ("cells", "meta")
GRID_STATES = frozenset((CELL_UNKNOWN, CELL_FREE, CELL_BLOCKED))

# 独立工程：所有数据都放在本项目目录内，不依赖外部路径。
#
# 采集/编辑**产物**统一收在 assets/ 下，根目录只留源码与"凭证类"目录：
#   assets/tiles/              瓦片数据（latest / run_* 会话 / maps 总图）
#   assets/grids2d/            2D 导航网格 npz
#   assets/items/map_auth/     地图标记数据（只此一份，公开口径已退场）
#   browser_profile/           Playwright 持久化登录配置（是登录态，不是产物）
#   configs/                   数美 dId 缓存（是设备标识，不是产物）
# browser_profile/ 与 configs/ 刻意留在 assets/ 外面：assets 语义上是"要分发的
# 资产"，而这两者绝不能随包发出去。

def default_data_root() -> Path:
    """数据根目录。

    默认取**当前工作目录**：产物跟着"你在哪运行"走，而不是跟着代码包走。
    以前默认取 ``__file__`` 所在目录，代码搬进 src/ 之后会指到包内部，
    所以统一改成 cwd + 显式覆盖（``--data-root`` / ``NAV_DATA_ROOT``）。
    """
    env = os.environ.get("NAV_DATA_ROOT", "")
    if env:
        return Path(env)
    return Path.cwd()


# 产物根目录：所有采集/编辑产物都收在这里（可用 NAV_ASSETS_DIR 覆盖）
def default_assets_dir(root: Path | None = None) -> Path:
    env = os.environ.get("NAV_ASSETS_DIR", "")
    if env:
        return Path(env)
    return (Path(root) if root else default_data_root()) / "assets"


# 瓦片根目录（可用环境变量 NAV_TILES_ROOT 覆盖）
def default_tiles_root(root: Path | None = None) -> Path:
    env = os.environ.get("NAV_TILES_ROOT", "")
    if env:
        return Path(env)
    return default_assets_dir(root) / "tiles"


# 持久化浏览器配置（登录态）目录（可用环境变量 NAV_PROFILE_DIR 覆盖）。
# 刻意**不**放进 assets/：它是登录凭证，不是分发的资产。
def default_profile_dir(root: Path | None = None) -> Path:
    env = os.environ.get("NAV_PROFILE_DIR", "")
    if env:
        return Path(env)
    return (Path(root) if root else default_data_root()) / "browser_profile"


TILE_SIZE = 256
RUN_PREFIX = "run"
DEBUG = bool(os.environ.get("NAV_DEBUG", ""))

# 瓦片 URL 格式: /tile(map02_1 之类)/<map>/<zoom>/<x>_<y>.png
# 组 1 必须用与别处一致的白名单：以前是 [^/]+，能匹配 `C:` 这种盘符，而
# map_name 会被拼进文件系统路径（session_dir / "C:/4/0_0.png" 在 Windows 上
# 会被 pathlib 当成绝对路径、丢掉基目录），等于把写入点交给远端页面摆布。
tile_pattern = re.compile(
    r"/tile(?:_[^/]+)?/([A-Za-z0-9_\-]+)/(\d+)/(-?\d+)_(-?\d+)\.png"
)
# 瓦片文件名: x_y.png
TILE_FILE_PATTERN = re.compile(r"^(-?\d+)_(-?\d+)\.png$")

#: 地图名同时用作目录名/文件名，统一在这里把关
MAP_NAME_RE = re.compile(r"[A-Za-z0-9_\-]+")
#: Windows 保留设备名：`NUL` 之类的名字能过白名单，但 mkdir 会直接失败
#: （exist_ok 只吞 FileExistsError，吞不掉 WinError 3）
_WIN_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)


def valid_map_name(map_name: object) -> bool:
    """地图名白名单：既挡路径分隔符/相对路径，也挡 Windows 保留设备名。"""
    s = str(map_name or "")
    if not MAP_NAME_RE.fullmatch(s):
        return False
    return s.upper() not in _WIN_RESERVED


def valid_map_zoom(map_name: object, zoom: object) -> bool:
    """地图名 + zoom 一起校验（二者都会进文件系统路径）。"""
    return valid_map_name(map_name) and str(zoom or "").isdigit()


# 模拟抓取参数
SIM_MAP = "sim"
SIM_ZOOM = "4"
SIM_COLS, SIM_ROWS = 10, 8
SIM_DELAY = 0.04  # 每张瓦片间隔（秒），让贴图过程肉眼可见

# =========================================================
# 小工具：无 Pillow 依赖的纯色 PNG 生成（模拟抓取用）
# =========================================================
