# 定位瓦片数据包 v3

`dataset.json` 仍使用 `okef-nav-dataset` v1。`localization/manifest.json` 和每张图的 `tiles.json` 升到 `schema_version: 3`。定位清单新增 `tile_layout: "zero-padded-roi"`。`coarse[]` 和 `floor_rank` 沿用 [v2](localization-tiles-v2.md) 的字段及文件。

每条 `tiles[]` 记录保留 `column`、`row`、`width` 和 `height`。宽高是该格的完整逻辑尺寸，含右边和底边的部分瓦片。`roi` 是块内非零匹配像素的半开外接矩形 `[left,top,right,bottom]`。矩形外的匹配 BGR 为 `[0,0,0]`。

```json
{
  "column": 0,
  "row": 0,
  "width": 512,
  "height": 512,
  "storage": "zero",
  "roi": [0, 0, 0, 0],
  "path": null,
  "size": 0,
  "sha256": null
}
```

下面是历史 v3 包中 `scene_indie_dg007_layer_tips_4` 的紧凑块，用于说明 ROI 坐标。解码文件得到 1×21 的 BGR，写入 512×512 块的第 407 至 427 行、第 0 列。当前楼层导出改为 `full` 以保留 alpha，此历史记录不再是当前发布示例。

```json
{
  "column": 6,
  "row": 4,
  "width": 512,
  "height": 512,
  "storage": "roi",
  "roi": [0, 407, 1, 428],
  "path": "objects/4d/4de51c418b55937a2def554984a2a9fb68ca41412bebcf98211b28b96ab635da.webp",
  "size": 82,
  "sha256": "4de51c418b55937a2def554984a2a9fb68ca41412bebcf98211b28b96ab635da"
}
```

| `storage` | 读取方式 |
| --- | --- |
| `zero` | 显式声明整块匹配 BGR 为零。直接填零。 |
| `full` | 解码 `path` 得到完整块。主图按 BGR 读取。楼层按原通道解码，将 `alpha == 0` 的 BGR 置零，缺省 alpha 为 255。`roi` 可用于跳过已校验的纯零子区。 |
| `roi` | `path` 解码为已经应用楼层 alpha 规则的无损 BGR，尺寸为 `(right-left,bottom-top)`。将其放回该块原位，再补零。 |

`full` 和 `roi` 的 `path` 相对 `localization/`，`size` 与 `sha256` 对应实际编码文件。每个格子恰有一条记录，缺少记录、实体文件缺失或校验失败均为资源错误。逻辑图尺寸、瓦片坐标、像素中心、标定、楼层 bounds 和导航注册与 v2 相同。

生产端根据解码后的匹配 BGR 计算 ROI，任一通道非零即保留。主图只有裁切后的无损文件比原文件小时才选用 `roi`，`full` 复用原编码字节。

楼层的覆盖范围独立于 BGR：不透明黑色与半透明像素同样需要保留。正式导出对非全透明楼层块使用 `full`，读取 alpha 时必须使用完整载荷，不能按 BGR ROI 裁剪。完全黑色但仍有覆盖的 `full` 块允许 `roi=[0,0,0,0]`；楼层 `zero` 仅表示全透明块。历史 `roi` 记录只能表达 ROI 内 alpha=255、外部 alpha=0，不能恢复原始部分透明度。需要从 v2 或原素材重建旧包；不能把 BGR 等价当作 alpha 等价。发布前同时校验两者。

正式导出从编辑器当前保存的素材和网格读取，页面或命令行均可运行。输入先冻结，交付前重新核对输入集合及内容摘要。需要完整的 `assets/tiles/presets/manifest.json` 与对应 Q90 瓦片、主图标定、分层目录及其图像、导航网格。主图使用预设和标定，楼层优先选同目录同名原始 PNG。WebP-only 源使用无损切块。源图尺寸、预设边界、标定和楼层声明需一致。

```powershell
uv run nav-grid-editor export-dataset --output output/nav-grid-release
```

已有 v2 对照包可以直接转换，用于验证精匹配裁边：

```powershell
uv run python -m nav_grid_editor.commands.compact_localization_native_tiles `
  --source output/localization-tiles-v2 `
  --output output/localization-tiles-v3
```

`validation.json` 按图记录各存储类型的格数、完整读取的解码像素、索引体积及像素等价结果。整包统计以清单及引用文件为准。图像索引用紧凑 JSON 写出，瓦片对象节省和索引节省分别计量。真实精匹配的耗时和峰值内存需通过消费端回放测量。

正式包的验证报告另含冻结输入摘要、全量精匹配 BGR 核验、粗搜与排序统计，以及采集目录记录的真实缺项。`missing_logic_ids` 中的楼层保留缺项身份。输出校验会拒绝孤立文件、重复坐标、缺块及无效的导航注册。可对交付目录再次运行校验：

```powershell
uv run python -c "from nav_grid_editor.services.maps.dataset_tiled_export import verify_tiled_release; verify_tiled_release('output/nav-grid-release')"
```
