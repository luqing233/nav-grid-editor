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
| `--grid2d-dir` | 2D 网格输出目录，默认 `<data-root>/assets/nav-grid-data` |

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
- **官方分层地图**：在已标定的地图上选择官方 `tier` 图层。分层图片首次访问
  自动下载到 `assets/tiles/layers/`，随后按官方世界 bounds 和当前标定矩阵
  叠到大地图坐标中。点击地图上的“分层入口”标记会直接展开对应的楼层列表。
  没有分层入口标记的区域归入“独立地图”，例如“首墩内部”和“遂明”。
  低缩放时只解码预览图，放大后才切换全分辨率，并只保留当前地图的解码缓存。
- **高性能渲染**：画布只绘制当前可见区域。打开地图时先加载长边不超过
  2048 像素的缩略图，缩放后再按需加载瓦片，避免大图占用过量显存。
  瓦片解码缓存限制为 420 张 / 96 MiB，并且切换到其他地图时立即释放；
  分层图低缩放使用预览版，放大后才加载全分辨率。拖动时 Canvas 位移和重绘
  在同一动画帧提交，并临时关闭侧栏背景模糊，避免拖拽抖动和闪烁。
- **地图标定**：在地图上选择特征点并输入游戏坐标 X/Z。系统使用仿射最小二乘
  和 RANSAC 计算像素与世界坐标的映射，结果写入对应地图的
  `<地图>_<zoom>_mapping.json`。
- **玩家定位**：标定完成后，服务端用 `configs/hg_content.txt` 里的凭证直接连
  官方地图 WebSocket `wss://ws.skland.com/ws/v1/game/endfield/map`，把实时坐标
  经 SSE 推给网页，显示玩家位置，不再依赖本地 WS 中继。`POST /api/coords`
  仍保留为外部客户端的备用上报口。
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

### 分层连接

网格支持类似 PCB via 的跨层连接。每个 grid 仍只保存 `Unknown/Free/Blocked`，
跨地图/分层连接统一保存在 `assets/nav-grid-data/links.json`：

- 起点和终点可以是大地图、分层地图或独立地图；
- 端点保存世界坐标区域（外加所属 `zoom`），不依赖易漂移的格子下标；
- 支持双向、单向、楼梯、电梯、传送、门、绳索和通行代价；
- 编辑器中进入“连接”模式，依次框选起点区域、切换分层、框选终点区域并保存；
  已存在的连接在“分层连接”面板里点“编辑”可以只重选端点，不必删掉重画。

保存时会校验两个端点**各自落在该层的可走格上**：端点整块落在网格范围外、或者
框在一片没涂过的格子上，都会拒绝保存并说明是哪一端、为什么。规划器只认“从可走格
走到可走格”，这种悬空的边等于通向虚空，所以必须挡在写入前。

读取时不改数据：已存在的连接如果端点无效，会在响应里带上 `checks`（`valid` 与
`issue`），编辑器把这条连接标红、列表里也写明原因，由用户决定怎么修。

`links.json` 目前是 `schema_version: 2`；v1 的端点没有 `zoom`，读出来会补成 `""`
并按查询的 `zoom` 解析（磁盘上的版本等下次写入才升上去）。

### 分层导航的约定

规划器按这几条组装分层图，编辑器负责保证产出的数据满足它：

1. **相邻关系只在同一层内成立** —— 每层各自的稠密网格就是该层的可走图；
2. **跨层一律走 `links.json` 的显式边** —— 绝不能把两层按 X/Z 叠起来当邻接。
   同一个 X/Z 上地表和地下都可能是可走的，叠加等于凭空连边；
3. **主地图不挖洞** —— 地表那一格是真实可走的，地下房间在它下面。把主图挖成
   `Blocked` 会删掉真实可走的表面；玩家在哪一层由 `floor_regions` 的 Y 区间判定。

于是「这张图能不能导航」= 所有连接端点有效 + 每处与其它层重合的区域都标了高度区间。
`GET /api/nav-graph?map=&zoom=` 直接把这件事判出来：

```json
{
  "ok": true, "map": "map02", "zoom": "4",
  "ready": false,
  "blockers": ["连接 … 端点无效：…", "重合区 21 格（大地图 / 测试区）…"],
  "tiers": [{"tier_id": "__main__", "grid": "map02_4.grid.npz",
             "origin": [0, 0], "cell_size": 1, "shape": [10, 10],
             "free_cells": [[0, 0]], "floor_regions": []}],
  "links": [{"id": "…", "from": {}, "to": {}, "kind": "stairs"}],
  "cross_tier_edges_from": "links"
}
```

`cross_tier_edges_from` 恒为 `links` —— 响应里不会出现任何隐式跨层邻接。

### 「坐标 → 第几层」的判定规则

`MapService.resolve_tier_by_height(map, x, y, z)` 用各层网格的 `floor_regions` 判断
玩家在哪一层。消费侧（ok-end-field）的跟随器要能独立复刻这条判定，不能依赖编辑器
进程，因此规则与测试向量是**跨仓库契约**：

- 规则说明与全部向量：`tests/data/floor_rule_vectors.json`（`schema_version: 2`）
- 执行器：`tests/test_floor_rule_vectors.py`

两边各跑同一份向量；任何一侧改了行为，另一侧会立刻失败。向量里 `contract: false`
的用例是当前实现的偶然行为（允许改动），失败时只打印提醒、不判失败。

**判定不看 `levelId`** —— 官方那个字段不可信（同一个 levelId 会被多个分层共用、
也可能滞后于玩家实际位置），所以候选层是「这张地图上所有声明了 `floor_regions`
的层」，主图只要自己标了区间同样是候选、没有特权。返回四种结果：

| status | 含义 | 附带 |
| --- | --- | --- |
| `unique` | 恰好一层盖住这点 | `tier_id` 是该层 |
| `ambiguous` | 多层盖住 | `matched` 列出全部，`tier_id` 为空 —— **不替调用方挑** |
| `main` | 兜底大地图 | `reason` = `no_layers`（一层都没标过）/ `no_region_covers`（标了但没盖住这点） |
| `invalid` | 地图名非法或坐标非有限数 | 这条位置消息本身不可用，**不该**据此切到大地图 |

几个容易踩的点（都在向量里）：

- **兜底是主路径，不是边角**：当前 map02 只有 2 段各 39 格区间，所以绝大多数坐标
  都走 `main`。`no_layers` 与 `no_region_covers` 要分开，界面上该说不同的话。
- **只有一个候选层也要它真的盖住这点** 才报 `unique`（v1 里是「单候选无条件
  unique」，那条 wart 在 v2 随 levelId 一起消失）。
- `region.cells` 非空时**先要求命中某个 cell 的 X/Z**，再判外框与 Y；都是闭区间。
- 区间非法（如 `x0 > x1`）时该层按「没有区间」处理，不抛错、不连累其它层。

### 「可走」只有一个来源

编辑器里所有连通性派生量（保存网格时的分块告警、孤立/被困诊断）都从
`web/static/js/grid/walk-policy.js` 里的 `WALK_POLICIES` 取判据，不再各自内联。

原因是踩过一次：分块告警过去用「节点认 `Free`、守卫只查 `Blocked`」这组**互不
匹配**的判据，于是报出来的块数在产品默认策略下偏少 —— map02 报 11 块、而规划器
（`allow_unknown=False`）看到 12 块，base01 报 5、看到 8。用户看到的数字和实际能
不能走过去对不上。**守卫判据必须与节点判据同源**：守卫更松时，斜向移动能借一个
未知格把两片可走区并起来。

两档策略各对应规划器的一档：`noUnknown` = `allow_unknown=False`（产品默认）、
`allowUnknown` = `True`。告警文案带上用的是哪一档，因为「分成几块」本身取决于
要不要冒穿过未知格的险，不是客观事实。

`tests/test_walk_policy_js.py` 用真实数据钉住这几个数字（主图 12 块 / 最大块
31918），并有一条反例：斜角相接、正交格是未知时，旧判据会把两块并成一块。

还要注意**块数会高估孤立**，而且偏差量是精确可算的：

```text
单格块数 = 真被困数 + 孤立数 + 假孤立数        （恒等，见测试）
```

「假孤立」指那格的对角有可行走邻居、而两个正交格被挡 —— 那是**我们自己的禁斜穿
规则造的孤立，游戏里人走得过去**。所以 `单格块数 == 被困 + 孤立` 等价于「假孤立
为 0」，是定理不是数据性质。实测 base01 是 `8 块 / 2 单格块 / 2 孤立 / 0 被困`
（假孤立 0），其余 4 张网格单格块全为 0。

结论：块数不能当「有多少格不连通」的另一种说法读，判断"某个格子是不是误点"要用
格子级的邻居事实（`walkableAnomalies`），不是块。

带分层的网格文件名为：

```text
<地图>__<tier>_<zoom>.grid.npz
```

未选择分层时继续使用旧格式：

```text
<地图>_<zoom>.grid.npz
```

**主图的 tier 有两种拼写，转换只走 `disk_tier` / `api_tier`（JS 侧是
`grid/tier-id.js` 的同名函数），别再内联判断：**

| 口径 | 值 | 用在哪 |
| --- | --- | --- |
| API | `"__main__"` | `/api/nav-graph`、`/api/grid2d/floor-map`、SSE 的 `gridTierId`、`/api/grid2d` 的 `tier` 参数 |
| 落盘 | `""` | 文件名与 `meta.tier`（`map02_4.grid.npz`，不带 tier 段） |

两种都必须存在：文件名里不能出现 `__main__`，而 API 里空串又表达不出「就是主图」
而不是「没选分层」。**少了转换是真的会出事** —— `valid_grid_tier("__main__")` 是
`True`（它符合地图名白名单），所以校验拦不住，落盘路径会拼出
`map02____main___4.grid.npz` 这种谁也不认识的伪文件名。这条转换以前散在 14 处内联
三元里，必然漏；现在 `tests/test_tier_spelling.py` 与
`tests/test_tier_id_js.py`（含一条源码级守卫）盯着它。

## 数据目录

```text
assets/
├── nav-grid-data/       导航网格数据，随仓库发布
├── tiles/
│   ├── layers/          官方分层图片缓存，仅本地
│   ├── latest/          最新合并瓦片，仅本地
│   ├── run_<时间戳>/    抓取会话，仅本地
│   └── maps/            拼接总图与标定，随仓库发布
└── items/               地图标记与图标，仅本地
```

GitHub 仓库只跟踪以下内容：

- `assets/nav-grid-data/`
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

分层网格还可以在 `meta.floor_regions` 中保存三维高度区间。服务端收到 WS
位置后，用这些区间判断玩家所在楼层：

```json
{
  "floor_regions": [
    {
      "x0": -768, "x1": 128,
      "z0": -768, "z1": 0,
      "y_min": 245, "y_max": 285
    }
  ]
}
```

只更新某个网格的楼层区间，不重存格子数据：

```powershell
$body = @{
  map = "map02"
  zoom = "4"
  tier = "scene_map02_lv005_layer_tips_7"
  floor_regions = @(
    @{ x0 = -768; x1 = 128; z0 = -768; z1 = 0; y_min = 245; y_max = 285 }
  )
} | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post -ContentType "application/json" `
  -Body $body http://127.0.0.1:8877/api/grid2d/floor-regions
```

SSE `coords` 事件会带上判定结果：`gridFloorStatus`（`unique` / `ambiguous` /
`main` / `invalid`）、`gridTierId`、`gridFloorReason`（`region_hit` / `overlap` /
`no_layers` / `no_region_covers` / `invalid_map` / `invalid_position`）与
`gridFloorCandidates`。命中多层时是 `ambiguous`，服务端不会随便选择楼层；没有分层
重叠时是 `main`（大地图），这才是当前数据下的常态 —— 判定规则见上文「坐标 → 第
几层」一节。
网格或连接模式中的“楼层区间”按钮会自动叠加同一区域的候选楼层：绿色为当前层，
红色为 X/Z 与 Y 都重合，黄色为只有 X/Z 重合。先选择“重合连通区”，再选择
“重合地图网格”，分别填写该层的 Y 范围并保存。所有涉及层级都已定义高度的
连通区会改为蓝色，与未处理的黄色重合区区分。主地图以 `__main__` 作为
特殊网格参与重合分析，可以与任意分层网格一起设置高度区间。

保存范围取“原数组范围与已涂格子范围的并集”，因此已有未知边框不会被裁掉。
当前限制：

- 单次编辑最多保存 `300,000` 个已涂格子。
- 稠密数组跨度最多为 `64,000,000` 个格子。
- 写入前会执行一次完整读回校验，校验失败时不会覆盖原文件。

### 下游使用

生成的网格可直接交由 ok-end-field 的导航模块读取：

```python
from src.nav.grid_io import load_grid

g = load_grid("assets/nav-grid-data/map01_4.grid.npz")
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

## 命令行分层地图抓取

使用自动发现程序抓取官方全部分层地图：

```powershell
uv run nav-grid-editor fetch-layers --dry-run
uv run nav-grid-editor fetch-layers
```

程序会：

1. 读取官方 `map/tree`；
2. 读取官方入口标记的 `levelId` / `targetRegionId`，精确绑定分层组；
3. 从当前官方前端 bundle 自动发现分层资源映射和资源版本；
4. 下载全部 tier PNG；
5. 生成 `assets/tiles/layers/catalog.json`，编辑器启动后直接读取。

重复执行时会跳过已存在图片；需要更新时使用：

```powershell
uv run nav-grid-editor fetch-layers --force
```

也可以绕过 CLI 直接运行：

```powershell
uv run python scripts/fetch_layers.py
```

输出结构：

```text
assets/tiles/layers/
├── catalog.json
├── map01/
│   └── map01_lv001/
├── map02/
│   ├── map02_lv002/
│   └── indie_dg007/
└── .bundle-cache/
```

编辑器显示名称：

| 内部 ID | 显示名称 |
| --- | --- |
| `base01` | 帝江号 |
| `map01` | 四号谷底 |
| `map02` | 武陵 |

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
│   ├── endfield_ws.py           官方地图 WS 位置流（认证复用 marks.py）
│   └── marks.py                 地图标记抓取、OAuth 签名与校验
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
