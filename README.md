# 终末地地图采集、标定与 2D 网格编辑

本地部署的地图工具，面向终末地地图数据采集与导航网格制作。提供瓦片抓取、
总图合成、像素与世界坐标标定、路径标记、坐标读取和 2D 无高度网格编辑能力。

项目主要面向 Windows。所有运行数据均保存在本地，Web 服务默认仅监听
`127.0.0.1`。

## 快速开始

### 环境要求

- Python 3.12 或更高版本
- [uv](https://docs.astral.sh/uv/)
- Windows 桌面环境，用于 Playwright 持久化浏览器
- 首次抓取前安装 Playwright Chromium

```powershell
git clone https://github.com/luqing233/nav-grid-editor.git
cd nav-grid-editor

uv sync
uv run playwright install chromium
uv run nav-grid-editor
```

启动后访问：

<http://127.0.0.1:8765>

根路径会自动跳转到 `/map`。

### 常用参数

| 参数 | 说明 |
| --- | --- |
| `--port` | HTTP 服务端口，默认 `8765` |
| `--data-root` | 数据根目录，默认当前工作目录 |
| `--tiles-root` | 瓦片目录，默认 `<data-root>/assets/tiles` |
| `--profile-dir` | Playwright 登录态目录，默认 `<data-root>/browser_profile` |
| `--grid2d-dir` | 2D 网格输出目录，默认 `<data-root>/assets/grids2d` |

示例：

```powershell
uv run nav-grid-editor --port 8765 --data-root D:/nav-data
uv run nav-grid-editor --grid2d-dir D:/nav-data/grids
```

对应环境变量包括 `NAV_DATA_ROOT`、`NAV_TILES_ROOT`、`NAV_PROFILE_DIR`
和 `NAV_GRID2D_DIR`。

> 数据根目录默认取当前工作目录。建议始终在项目根目录启动，或显式传入
> `--data-root`。

## 功能

- **地图抓取**：通过 Playwright 打开游戏地图页面，拦截瓦片请求并实时写入
  浏览器画布。已存在的瓦片直接使用本地缓存；抓取结束后自动合并到
  `assets/tiles/latest`。首次使用需要在弹出的浏览器中完成登录。
- **模拟抓取**：在内存中生成瓦片并实时演示拼接过程，不写盘，也不需要登录。
- **总图合成**：将当前地图与层级的所有瓦片合成为 PNG，保存到
  `assets/tiles/maps/<地图>/<zoom>/<地图>_<zoom>.png`，同名文件覆盖。
- **高性能渲染**：画布只绘制当前可见区域。打开地图时先加载长边不超过
  2048 像素的缩略图，缩放后再按需加载瓦片，避免大图占用过量显存。
- **地图标定**：在地图上选择特征点并输入游戏坐标 X/Z。系统使用仿射最小二乘
  和 RANSAC 计算像素与世界坐标的映射，结果写入对应地图的
  `<地图>_<zoom>_mapping.json`。
- **玩家定位**：标定完成后，通过 `POST /api/coords` 或兼容的 WebSocket
  中继发送世界坐标，网页会实时显示玩家位置。
- **路径标记**：粘贴 `(x,z)` 坐标序列后生成路径折线。起点为绿色，终点为
  红色，途经点显示编号。
- **坐标读取**：点击地图即可查看游戏世界坐标，以及对应的 2D 网格格子坐标。
- **地图标记**：支持抓取认证口径的官方地图标记，包括玩家自建结构。标记筛选
  面板在浏览、网格和取坐标模式下可用，标定模式下隐藏。
- **2D 网格编辑**：在已标定的地图上绘制 `Free`、`Blocked` 和未知格子。
  支持画笔宽度、区域框选和批量填充。

### 网格状态

| 状态 | 值 | 含义 |
| --- | ---: | --- |
| 未知 | `0` | 尚未探索。下游规划通常将其视为高代价区域 |
| Free | `1` | 已确认可通行 |
| Blocked | `2` | 障碍或不可通行区域 |

不要把未探索区域标记为 `Free`，也不建议将整个未知区域标记为 `Blocked`。

## 数据目录

```text
assets/
├── grids2d/             2D 网格，随仓库发布
├── tiles/
│   ├── latest/          最新合并瓦片，仅本地
│   ├── run_<时间戳>/    抓取会话，仅本地
│   └── maps/            拼接总图与标定，随仓库发布
└── items/               地图标记与图标，仅本地
```

GitHub 仓库只跟踪以下内容：

- `assets/grids2d/`
- `assets/tiles/maps/`

瓦片缓存、抓取会话、地图标记、图标和缩略图缓存均保留在本机。
`browser_profile/` 与 `configs/` 包含登录态和设备标识，始终不会进入版本库。

抓取会话目录是增量目录，不是完整快照。读取瓦片时，系统会合并抓取中的会话、
`latest`、历史会话和旧版扁平目录；同一坐标以较新的来源为准。

## 2D 网格格式

网格以稠密 `uint8` NPZ 保存，文件名为 `<地图>_<zoom>.grid.npz`。
文件仅包含两个成员：

| 成员 | 类型 | 说明 |
| --- | --- | --- |
| `cells` | `(H, W)` `uint8` | 行对应世界 z，列对应世界 x |
| `meta` | JSON 字符串 | 标定原点、格子尺寸和格式版本 |

`meta` 包含 `origin`、`cell_size`、`magic`、`schema_version` 和
`axis_convention`。`origin` 表示 `cells[0,0]` 最小角的世界坐标。

保存范围取“原数组范围与已涂格子范围的并集”，因此已有未知边框不会被裁掉。
当前限制：

- 单次编辑最多保存 `300,000` 个已涂格子。
- 稠密数组跨度最多为 `64,000,000` 个格子。
- 写入前会执行一次完整读回校验，校验失败时不会覆盖原文件。

### 下游使用

生成的网格可直接交由 ok-end-field 的导航模块读取：

```python
from src.nav.grid_io import load_grid

g = load_grid("assets/grids2d/map01_4.grid.npz")
print(g.shape, g.counts(), g.extent())
```

读取端会校验格式、版本、维度、值域和坐标参数。不符合规范的文件会明确报错，
不会以未知格或错位坐标的形式静默载入。

## 命令行地图标记

除网页操作外，也可以通过主命令抓取认证地图标记：

```powershell
uv run nav-grid-editor fetch-marks --help
uv run nav-grid-editor fetch-marks --per-level
```

默认读取 `configs/hg_content.txt`。该文件包含认证凭证，不会进入版本库。

## 项目结构

```text
src/nav_grid_editor/
├── cli.py                       命令入口
├── api/
│   └── app.py                   FastAPI 应用与路由
├── commands/
│   └── fetch_marks.py           命令行地图标记抓取
├── services/
│   ├── maps/                    瓦片、事件、网格、标定与服务编排
│   └── marks.py                 地图标记抓取与校验
├── integrations/smsdk/          设备 ID、指纹与 SMSdk 运行资源
└── web/
    ├── map_composer.html        页面结构
    └── static/
        ├── css/                 页面样式
        └── js/
            ├── map_composer.js  应用入口与业务编排
            ├── grid/            分块网格文档与位图缓存
            └── render/          栅格图层调度
```

依赖方向为 `cli -> api -> services -> integrations`。服务层不依赖 FastAPI，
浏览器资源由 `/static/` 提供。

## 开发与测试

```powershell
uv sync --locked
uv run python -m unittest discover -s tests -t . -v
uv lock --check
uv build
```

如果无法直接执行 `nav-grid-editor`，请使用 `uv run nav-grid-editor`，避免依赖
系统 Python 或全局 PATH。
