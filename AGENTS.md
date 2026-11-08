# AGENTS.md

终末地地图采集/标定/2D 网格编辑工具。`README.md` 是权威文档（中文），先读它；
本文件只记容易踩的坑。

## 命令

- 一律用 uv：`uv sync --locked`、`uv run nav-grid-editor`（入口 `src/nav_grid_editor/cli.py`）。
- 测试是 unittest，无 pytest / lint / typecheck / CI：
  - 全量：`uv run python -m unittest discover -s tests -t . -v`
  - 单个：`uv run python -m unittest tests.test_grid2d -v`
  - 发布前：`uv lock --check`、`uv build`
- 前端测试（`tests/test_*_js.py`）用 Node 执行真实 ESM 模块；缺 node 整类 skip，
  别把 skip 当通过。`tests/test_walk_policy_js.py` 还依赖子模块的真实网格数据。
- 手动检查 JS 语法必须 `node --input-type=module --check`（`node --check` 对 ESM
  语法错误返回 0）。
- 服务默认端口以 `cli.py` 的 `DEFAULT_PORT` 为准，README 参数表可能滞后。

## Git

- `assets/nav-grid-data/` 是子模块（独立仓库）。改网格 / `links.json` / `levels.json`
  必须先在子模块内提交，再回主仓提交指针；`assets/tiles/maps/` 直接在主仓跟踪。
- 工作区文件多为 CRLF、索引为 LF，`git status` 会把约 25 个文件显示为全文件改动。
  看真实差异用 `git diff --ignore-cr-at-eol`，不要提交纯换行变更。
- 提交信息用中文 Conventional Commits（`feat(scope): …`）。
- `configs/`（凭证/设备 ID）与 `browser_profile/`（登录态）已 gitignore，不要提交。

## 架构与约定

- 依赖方向 `cli -> api -> services -> integrations`；services 不得 import FastAPI。
  `api.app.service` 全局由 cli 注入。
- 新增 `web/static/js/<新子目录>/*.js` 必须在 `pyproject.toml` 的
  `[tool.setuptools.package-data]` 白名单补 glob，否则 dev 正常、wheel 404
  （`tests/test_project_layout.py` 会拦）。
- 主图 tier 两种拼写：API `"__main__"` / 落盘 `""`，转换只走 `disk_tier` /
  `api_tier`（JS：`grid/tier-id.js`），别内联三元。
- 「可走」判据唯一来源是 `web/static/js/grid/walk-policy.js` 的 `WALK_POLICIES`；
  连通性诊断不得另写判据。
- `tests/data/floor_rule_vectors.json` 是与 ok-end-field 的跨仓库契约
  （坐标 → 楼层判定），改语义前先读 `tests/test_floor_rule_vectors.py`。
