# 终末地地图采集 / 标定 / 网格编辑工具

独立的本地网页工具：地图瓦片实时采集、拼合总图、像素↔游戏坐标标定、
2D 无高度网格编辑、路径标记、取坐标。

## 使用方法

在项目目录下启动，任选一种：

```powershell
uv run nav-grid-editor                 # 推荐：uv 自动同步依赖后启动
.venv\Scripts\nav-grid-editor.exe      # 已经 uv sync 过的话，直接双击/运行也行
.venv\Scripts\activate                 # 或者先激活虚拟环境
nav-grid-editor
```

启动后浏览器打开 <http://127.0.0.1:8765>（自动跳转到 `/map`）。

> 本项目的 Python 只装在 `.venv/` 里，没有写进系统 PATH，所以 `python main.py`
> 这类命令是跑不起来的；请用上面三种方式之一。

> 数据根目录默认取**当前工作目录**，所以请在项目目录里启动。采集/编辑**产物**
> 统一收在根目录下的 `assets/`（`assets/tiles/`、`assets/grids2d/`、
> `assets/items/`）；`browser_profile/`（登录态）与 `configs/`（设备ID）是凭证类
> 目录，刻意留在 `assets/` 外面。换数据根目录用 `--data-root` 或环境变量
> `NAV_DATA_ROOT`。

常用参数：

```powershell
nav-grid-editor --port 8765 --data-root D:/some/data-root
nav-grid-editor --grid2d-dir D:/some/path/grids   # 只改 2D 网格目录（NAV_GRID2D_DIR）
```

> **2D 网格以稠密 npz 落盘**，文件名 `<地图>_<zoom>.grid.npz`（如 `base01_4.grid.npz`），
> 就是 ok-end-field 导航规划的运行时格式，可被它直接读取。
> `origin` / `cell_size` 都内嵌在文件的 `meta` 里，保存时按「原数组范围 ∪ 已涂格子范围」
> 确定（新建网格才退回已涂格子的边界盒），不需要外部坐标基准文件。

## 代码结构

```text
nav-grid-editor/
├── pyproject.toml           打包 / 依赖 / 入口（唯一事实来源）
├── README.md
├── src/nav_grid_editor/
│   ├── cli.py               唯一入口：nav-grid-editor / python -m nav_grid_editor
│   ├── api/
│   │   └── app.py           HTTP 层：FastAPI 应用与全部路由
│   ├── commands/
│   │   └── fetch_marks.py   命令行抓取地图标记
│   ├── services/
│   │   ├── maps/            瓦片、事件、网格、标定与服务编排
│   │   └── marks.py         地图标记抓取与校验
│   ├── integrations/smsdk/  设备 ID、指纹与 SMSdk 运行资源
│   └── web/
│       ├── map_composer.html  页面结构
│       └── static/            CSS / JavaScript 静态资源
├── tests/                   回归测试（unittest）
├── assets/                  运行数据
│   ├── grids2d/             2D 网格，随仓库发布
│   ├── tiles/maps/          拼接总图与标定，随仓库发布
│   ├── tiles/latest|run_*/  瓦片缓存与抓取会话，仅保留本地
│   └── items/               地图标记与图标缓存，仅保留本地
└── browser_profile/         Playwright 登录配置（登录态，不进 assets/）
```

依赖方向保持单向：`cli -> api -> services -> integrations`。`services` 不依赖
FastAPI，页面通过 `/static/` 读取 CSS/JavaScript；认证资源只由 SMSdk 集成层访问。

## 功能

- **合成总图**：选择地图/zoom 后点击“合成总图”，已有的瓦片会实时刷新到画布上；
  下拉框选择地图/zoom 会立即显示该地图；总图可用鼠标拖动平移、滚轮缩放（以光标为中心）、
  双击/“适应窗口”按钮还原；下载总图取服务端保存的原始 PNG。
  服务端同步保存到 `assets/tiles/maps/<地图>/<zoom>/<地图>_<zoom>.png`（同名覆盖只保留一份）。
- **渲染方式（大图不卡的关键）**：画布只有一屏大小，永远只画“当前可见区域”。
  打开地图先用服务端生成的缩略图打底（`/api/overview`，长边 ≤ 2048 的 WebP，
  约一两百 KB），放大到能看清细节时才按需拉取可见范围内的瓦片；平移/缩放不重建
  画布，也不会因为整图尺寸（例如 map02@4 是 10752×15872）而爆显存。
  缩略图缓存在 `assets/tiles/maps/<地图>/<zoom>/<地图>_<zoom>.overview.webp`，可随时删除。
  **它是“合成总图”时顺手生成的**——那时整张图已经在内存里，所以打开地图不用等；
  只有在别处产生总图、或你手动删掉缩略图时，才会去解码那张几十 MB 的 PNG
  （map02@4 冷生成约 4.6 秒，日志里会打出用时）。
- **开始抓取**：用 Playwright 打开游戏地图页面（非无头模式下弹窗自动最大化、
  页面自适应窗口大小），拦截瓦片请求并实时贴到画布上，新瓦片自动落盘到
  `assets/tiles/run_<时间戳>/` 会话（已下载过的瓦片直接本地应答、不再请求网络，
  每次抓取只抓取未下载过的），结束后自动合并到 `assets/tiles/latest`。
  首次使用需在弹出的浏览器里登录一次，登录态保存在本项目 `browser_profile/` 下。
- **模拟抓取**：不落盘、纯内存演示实时贴图效果，无需登录。
- **地图标定（建立坐标系）**：点“◎ 标定”进入标定模式，点击地图上的特征点、
  输入游戏坐标 X/Z（建议 ≥3 个、尽量分散），点“计算并保存标定”：
  用仿射最小二乘 + RANSAC（剔除误点）算出 像素↔游戏坐标 映射，保存到
  `assets/tiles/maps/<地图>/<zoom>/<地图>_<zoom>_mapping.json`（与控制点误差一起显示；
  格式与 wsserver 的 map_calibrator 兼容，旧标定文件可直接识别）。
- **玩家定位**：地图标定后，`POST /api/coords`（或 `ws://127.0.0.1:3001`）上报的
  游戏坐标会实时换算成地图像素，画布上显示红色玩家标记（含坐标标签）。
- **路径标记**：粘贴坐标串如 `(-97.2,6.0) -> (-91.2,6.0) -> ...`（括号坐标或 x,z 对
  均可，自动忽略 `->` 和文字标签），点“标记路径”即在标定地图上画出折线路径：
  起始点绿色、终点红色、途经点橙色带编号；每个地图的路径独立保存，清除按钮可移除。
- **取坐标**：点“◎ 取坐标”后点击地图任意位置，立即显示该点的**游戏世界坐标 (x, z)**
  和 2D 网格格子坐标 [格子X, 格子Z]（蓝色标记点带坐标标签）；未标定的地图显示像素坐标。
- **网格编辑（2D，无高度）**：标定好的地图上点“✎ 编辑网格”，**左键点击/拖动放置
  Free / Blocked / 未知格子**（画笔宽度可调 1~50 格；**Shift+左键拖动框选**矩形区域后
  可批量填充，Esc 取消选择；右键拖动平移）。三态的含义（下游规划器就按这个理解）：
  **Free = 确认可走**、**Blocked = 墙/不可通行**、**未知 = 没探过**。规划器把未知当作
  “可冒险、代价更高”，所以**别**把未探区域刷成 Free（会穿墙），也**别**刷成 Blocked
  （会无路可走）。画笔里的“未知”即**擦除**（把格子退回未探状态）。
  编辑面板实时显示 Free / Blocked / 未知 的数量，并把**保存范围**用淡色加黄框画出来——
  那就是保存后文件的真实范围（范围内未涂色的格子会被存成未知；跨度超过 400 万格会被
  拒绝保存）。打开已有网格时保存范围取「文件里的原范围 ∪ 已涂格子范围」，只扩不缩，
  未知边框不会被裁掉；涂到范围外会自动扩图。
  **保存网格**写出稠密 `uint8` npz，命名 `<地图>_<zoom>.grid.npz`（如 `base01_4.grid.npz`），
  固定落在 `assets/grids2d/`（`--grid2d-dir` / `NAV_GRID2D_DIR` 可改）。文件恰好两个成员：
  `cells`（`(H, W)` 稠密数组，`0=未知 / 1=可走 / 2=阻挡`，**行对应世界 z、列对应世界 x**）
  与 `meta`（内嵌 JSON，含 `origin`、`cell_size`、`magic`、`schema_version`）。
  `origin` 是 `cells[0,0]` **最小角**的世界坐标；读取时的校验与 ok-end-field 的读方同级
  （npz 成员集合、`cells` dtype、值域、`meta` 必须是字符串数组、`magic`/`schema_version`、
  `axis_convention`），落盘前还会读回自检一次，所以写出的文件必然能被规划器读入。
  因此**面板里显示的格子下标是相对保存范围最小角的**，不再是旧版的绝对网格索引。
- 页面右侧有新增/更新/未变化统计与实时日志。

## 数据目录

```text
nav-grid-editor/
└── assets/
    ├── tiles/
    │   ├── latest/        最新合并瓦片，仅本地
    │   ├── run_<时间戳>/  每次抓取会话，仅本地
    │   └── maps/          拼接总图与标定，随仓库发布
    ├── grids2d/           2D 网格，随仓库发布
    └── items/             地图标记与图标，仅本地
```

GitHub 仓库只跟踪 `assets/grids2d/` 和 `assets/tiles/maps/`。瓦片缓存、抓取会话、
标记数据及缩略图缓存均保留在本机，不进入版本库。

如需沿用旧 wsserver 工程的数据，把它的 `tiles` 文件夹内容拷入本项目 `assets/tiles/` 即可（目录结构一致，可直接识别）。
也可用 `--tiles-root` / `--profile-dir`（或环境变量 `NAV_TILES_ROOT` / `NAV_PROFILE_DIR`）指定其他位置。

> 抓取会话目录（`run_*`）只存本次**新增/变化**的瓦片：没变化的瓦片直接复用本地缓存、
> 不重复写盘，所以它是增量而不是快照。合成总图、边界统计、`/tiles/...` 取图一律按
> **所有目录的并集**处理，同一坐标以更新的为准（优先顺序：抓取中的会话 → `latest` →
> 更早的会话 → 旧的扁平目录）。

## 输出对接（下游消费）

`assets/grids2d/<地图>_<zoom>.grid.npz` 就是 **ok-end-field 导航规划的运行时格式**，
放到它期望的位置（默认 `assets/nav/`）即可直接使用，**不需要任何转换脚本**：

```python
from src.nav.grid_io import load_grid
g = load_grid("assets/grids2d/map01_4.grid.npz")
print(g.shape, g.counts(), g.extent())
```

`load_grid` 会严格校验 `magic` / `schema_version` / 数组形状 / 值域 / `cell_size > 0`，
不符就直接报错——所以本工具的产出**要么被正常读入，要么明确失败**，不会静默读出错位的网格。
本工具导出前会自检同样的条件，因此正常保存的文件必然能读回。

## 依赖安装（仅 Windows 上需要一次）

```powershell
uv sync                        # 建 .venv、装依赖，并注册 nav-grid-editor 入口
uv run playwright install chromium  # 首次需要：下载抓取用的 Chromium
uv run nav-grid-editor         # 启动（等价于 .venv\Scripts\activate 后执行 nav-grid-editor）
uv run nav-grid-editor fetch-marks --help  # 可选：命令行抓取地图标记
```

用 pip 的话：`pip install -e .`（装依赖并注册入口；也可以直接用 `python -m nav_grid_editor`
启动）。没有 playwright 时抓取按钮会提示错误，其他功能不受影响。
