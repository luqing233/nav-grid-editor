# 定位瓦片数据包 v1

从完整的浏览器 Q90 预设复制主图瓦片，从原始 PNG 楼层图按原像素切块。导出时以瓦片解码像素计算匹配缩图。消费端可以从包内索引直接读图和区域，运行所需的文件全部在包内。

```powershell
uv run python -m nav_grid_editor.commands.build_localization_tiles `
  --presets assets/tiles/presets `
  --snapshot output/webp-data-pr `
  --original-layers assets/tiles/layers `
  --output output/localization-tiles-v1 `
  --compare-legacy
```

`--snapshot` 接受 `okef-visual-localization` v1 快照，提供标定、目录、网格、层声明和跨层连接。普通导出读取这些元数据及导航文件。开启 `--compare-legacy` 时才读取旧整图，另写 `comparison.json`。该报告逐图比较每 8 像素采样的颜色误差，以及两侧像素差在瓦片边界上的变化。数值单位为 8 位颜色级差，供回放排查使用。

主图预设的来源是已保存总图，补齐的是该总图的矩形像素范围。每张预设瓦片必须存在，字节、尺寸和摘要必须匹配，缺失时导出失败。楼层切块使用原始 PNG，避免对已发布 Q90 楼层图再次有损编码。每块的 alpha 与原图逐像素核对。旧包与瓦片包的 RGB 可能不同，尤其是接缝附近。定位精度以消费端回放验证为准。

## 包入口和文件引用

| 文件 | 版本及内容 |
| --- | --- |
| `dataset.json` | `okef-nav-dataset` v1。沿用导航文件、层身份、坐标空间、标定和网格注册关系。`registrations[].visual.image` 指向瓦片索引 JSON。此字段的解释由定位清单的 `kind` 决定。 |
| `localization/manifest.json` | `okef-tiled-localization` v1。`base_maps[].image` 指向主图索引，`layer_catalog` 指向楼层目录。`files` 为包内所有定位资源的 `path`、`size`、`sha256`。 |
| `localization/layers/catalog.json` | 原楼层目录 v1。`layers[].path` 相对 `layers_root`，指向楼层索引。保留原稳定 ID、地图归属、尺寸、bounds、分类及 `missing_logic_ids`。 |
| `localization/**/tiles.json` | `okef-image-tiles` v1。每图的瓦片、标定、匹配尺度和缩图。 |
| `localization/objects/` | Q90 WebP 对象，按 SHA-256 寻址。多个位置可以共享同一对象，包括显式列出的纯空白瓦片。 |

`dataset.json` 中的路径相对包根目录。定位清单和图像索引中的资源路径相对 `localization/`，楼层目录的 `path` 除外。摘要是编码后文件的 SHA-256，大小以字节计。网格、连接、层声明和主图标定按输入快照原字节保留。

## 每图索引

`id` 是主图 ID 或楼层 ID，`map_id` 是所属主图，`zoom` 为字符串。`width` 和 `height` 是原生逻辑像素尺寸。主图包含 `mapping`，楼层包含 `bounds`、`level_id` 和 `map_category`。主图 `source.tile_origin` 记录抓取坐标系中左上块的 `[x,y]`，用于追溯。该偏移不参与标定换算。

左上块为 `(column=0,row=0)`，列向右、行向下，像素中心为整数坐标。每条 `tiles[]` 记录 `column`、`row`、`width`、`height`、`path`、`size` 和 `sha256`。块的左上像素为 `(column*tile_size,row*tile_size)`。主图边长为 256，楼层为 512，右边和底边用实际尺寸。`complete: true` 要求覆盖矩形中的每个位置恰好一次，包括黑色或透明块。`missing_logic_ids` 单独描述尚未采集的楼层。

## 匹配坐标与缩图

主图保持原标定，按现有定位器的约 4 px/m 约定取图。楼层保留各自原生尺寸。令 `sx=(east-west)/width`、`sy=(north-south)/height`，则 `match_raster.source_scale=[4*sx,4*sy]`，`width`、`height` 是 OpenCV 归一化后的尺寸。匹配像素中心 `q` 对应原图中心 `(q+0.5)/scale-0.5`。原图 `u,v` 转世界坐标使用 `X=west+sx*u`、`Z=north-sy*v`。读归一化 ROI 时应按全图同一原点取样，跨接缝拼接所需源像素后再做面积重采样。

- `coarse[]` 覆盖 1280、1600、1920、2560、3440、3840 六个截图宽度。每条有 `screen_width`、`scale`、`scale_hex`、实际 `width`、`height` 及文件记录。使用 `scale = width / (4 * 1706.5) * 0.5`，与 `minimap_map_scale(width) * COARSE_FACTOR` 一致。尺寸和映射以传给 OpenCV 的 `fx/fy` 为准，不能用缩图四舍五入后的尺寸反推比例。
- 主图从完整瓦片像素直接执行 `INTER_AREA`。楼层先将 alpha 为零的 BGR 置黑，按 `match_raster.source_scale` 做 `INTER_AREA`，再按粗搜比例做第二次 `INTER_AREA`。部分透明像素仍保留颜色，和现有定位器一致。
- 使用 OpenCV 解码时，彩色瓦片为 BGR 或 BGRA，缺省 alpha 为 255。WebP 编码可以省略整块不透明的 alpha 通道。
- `floor_rank` 为楼层排序图，主图为 `null`。楼层先对原生 RGB/RGBA 瓦片栅格做 `INTER_AREA`，尺寸是 `max(1, round(世界跨度*0.5))`，然后将输出中 alpha 为零的颜色置黑。粗搜与排序缩图均以无损 WebP 保存。
- 非默认宽度或比例常数没有对应的预生成粗搜图。消费端需要用完整瓦片重算该比例，或显式报告该比例未准备。不要用相邻宽度的缩图代替。

导出端每次在内存中构建一张图的像素用于全局面积采样，主图瓦片字节直接复制。正式切换前使用失败截图回放，检查主图候选、重叠楼层竞争和连续帧确认。
