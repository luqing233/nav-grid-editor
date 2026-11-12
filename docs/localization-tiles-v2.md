# 定位瓦片数据包 v2

`dataset.json` 沿用 `okef-nav-dataset` v1。`localization/manifest.json` 的 `okef-tiled-localization` 和每个 `tiles.json` 的 `okef-image-tiles` 升到 `schema_version: 2`。`search.image_layout` 为 `zero-padded-roi`。主图和楼层原生瓦片仍按 [v1 契约](localization-tiles-v1.md) 读取。

`coarse[]` 和非空的 `floor_rank` 中，`width`、`height` 是完整搜索栅格尺寸。`roi` 是在该栅格中的半开矩形 `[left,top,right,bottom]`，`fill` 固定为 BGR `[0,0,0]`。WebP 存储矩形内部的无损像素，解码尺寸为 `(right-left,bottom-top)`。`path` 相对 `localization/`。`size` 和 `sha256` 对应紧凑文件。

```json
{
  "width": 576,
  "height": 648,
  "roi": [283, 386, 424, 529],
  "fill": [0, 0, 0],
  "path": "layers/map02/map02_lv004/scene_map02_lv004_layer_tips_1/coarse-1920.webp",
  "size": 12456,
  "sha256": "5e479bb8cef104cac6908f478906b89ef4f7ae08483abe39cf84559427755b00",
  "screen_width": 1920,
  "scale": 0.14063873425139173,
  "scale_hex": "0x1.2007336148d50p-3"
}
```

上面的记录来自栖云窟样包。全黑栅格使用 `roi: [0,0,0,0]`、`path: null`、`size: 0`、`sha256: null`，保留完整尺寸。有效像素的外接矩形覆盖整幅时使用 `[0,0,width,height]`。主图 `floor_rank` 仍为 `null`，与存在但全黑的楼层排序栅格可区分。非空 ROI 的文件缺失、摘要错误或解码失败属于资源错误。

裁边在 v1 的全部 alpha 和 `INTER_AREA` 步骤之后执行。有效像素是任一 BGR 通道非零的像素。把 ROI 放回原位并补零即可得到与 v1 逐像素相同的栅格。缩放比例、标定和搜索坐标均以完整栅格为准。原生瓦片的覆盖和引用保持 v1 格式。

消费端加速匹配时，按实际模板宽和高分别向 ROI 两侧扩展至少 `模板尺寸−1`，并裁到完整栅格边界。候选坐标须加回搜索区域原点。完全落在零区的窗口仍参与零分和并列候选语义，结果有歧义时用完整零填充栅格复核。

消费端可直接将 ROI 补零为完整栅格，并交给现有匹配器。这是候选结果需要保持一致时的读取方式。ROI 限域匹配还需要回放验证低分峰、并列峰和边缘窗口。

```powershell
uv run python -m nav_grid_editor.commands.compact_localization_tiles `
  --source output/localization-tiles-v1 `
  --output output/localization-tiles-v2
```

输出为独立数据包。`validation.json` 逐图记录两版面积、文件字节数、ROI 和补零后的像素等价结果。包体统计以 `dataset.json`、定位清单及两者引用的文件为准，验证报告单独计算。
