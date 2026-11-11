# nav-grid-data 数据包与上传约定

统一清单 v1 已用于编辑器“一键导出”。当前 ok-end-field 仍从兼容目录加载资源，
不会因为存在 dataset.json 自动下载、切换快照或忽略旧配置。JSON Schema 只约束清单结构，
不证明定位精度、导航可达或全部游戏区域已经覆盖。

## 如何导出

1. 先保存标定与网格，确认实际分层 catalog 及引用图片已抓取到本机。
2. 点击编辑器顶部“导出数据包”，填写新目录或空目录；留空会生成独立 exports 子目录。
3. 点击“一键导出 nav-grid-data”，在状态区查看进度及最终目录。
4. 只从已完成的导出目录准备提交。导出失败不交付半包；原素材、原网格及非空目标保持原状。

导出的是所有已保存磁盘数据，不是当前画布截图。已有非空目录（包括 Git 仓库）不会被直接覆盖。
需要更新现有 nav-grid-data 仓库时，把导出目录作为完整目标快照，对照旧版本复制变化文件，
显式审查需要删除的旧资源；不要只叠加新文件而留下已移除的 grid。工具不自动 commit/push。

原始彩色素材仍在编辑器的数据目录内；发布包图片统一保存为原分辨率彩色 WebP Q90，
使用 Pillow `quality=90, method=4, lossless=False`，RGB 或 RGBA。不转灰度、不缩放或重采样，
楼层图保留原 alpha，逐像素验证 alpha 无损；不把透明背景压成黑色。RGB 为有损编码，不声称逐像素相等。
NPZ 保持原始字节，JSON 输出规范 UTF-8/LF 后计算 SHA-256。
旧 PNG 输入继续兼容；同名主图优先选择 WebP。清单字段及 schema_version 不变，
catalog.path / filename、mapping.map_name、manifest 和 dataset 中的路径同步指向实际 WebP，摘要按输出重算。
消费端按清单取图，以 OpenCV IMREAD_COLOR 读取基础图、IMREAD_UNCHANGED 读取含 alpha 的楼层图；
不得再强制单通道灰度或 RGB 三通道相同。截图中纯白及周围 2 px 的蒙版由定位算法处理，不写入地图资源。
WebP 单边上限 16383 像素，超限拒绝整个导出，不做隐式缩放。发布时 WebP 同样纳入 Git LFS。

## 目录与内层格式

```text
nav-grid-data/
  dataset.json                         本次完整快照的统一发布清单
  schemas/dataset.schema.json           根清单结构约束
  levels.json                          全部层声明，包含没有网格的层
  links.json                           显式跨层连接
  <map>_<zoom>.grid.npz                 主图网格
  <map>__<tier>_<zoom>.grid.npz          分层网格
  localization/
    manifest.json                      既有定位清单，不更换其schema
    maps/<map>/4/<map>_4.webp
    maps/<map>/4/<map>_4_mapping.json
    layers/catalog.json
    layers/<map>/<level>/<tier>.webp
```

网格继续使用 magic=okef-nav-grid/schema_version=2：cells[z,x] 为二维 uint8，
0 未知、1 可走、2 阻挡；meta 中 origin 是最小角、cell_size 是世界单位/格。
层声明仍由 levels.json schema 1 提供，不能用实际 NPZ 列表代替。
links 兼容 v1/v2/v3；floor_regions、端点 rect、可选端点高度均为世界坐标。
缺失 Y 仍为未知，二维网格 origin[1]=0 不是玩家高度。

## 唯一清单契约

以随编辑器分发的 `services/maps/dataset.schema.json` 和导出包内同版 schema 为准。
数据仓提案的 examples/dataset.example.json 是 a78e109 基线的格式示例，不是额外正式发布版本。

| 字段 | 含义 |
|---|---|
| schema_version / kind | 固定 1 / okef-nav-dataset |
| dataset_id | 数据集的稳定身份，当前 endfield-nav；扩区域不改名 |
| release_id | 本次导出的独立发布ID；不填包含自身文件的 Git SHA，避免自引用 |
| parent_release_id | 可为 null；只记谱系，不表示差分包依赖，当前导出默认 null |
| coordinate_spaces | 稳定坐标空间：id、kind(world/independent)、map_name、unit；独立场景另有 level_id |
| declarations | 完整层声明文件路径，当前 levels.json |
| navigation_files | NPZ、levels、links 的 path/size/sha256，另有 role=grid/levels/links |
| localization_manifest | 现有定位子清单的 path/size/sha256；纯网格提案可为 null |
| registrations | 关联坐标空间、地图、层、zoom，以及可空 visual、可空 grid |

registration 的唯一键是 `(coordinate_space_id, map_name, tier, zoom)`。
主图 tier 只保存磁盘空串；API 边界经 api_tier/disk_tier 转换为 __main__，不再存第二份同义字段。
zoom 是字符串资源档次，和 NPZ 的 cell_size 无换算关系。当前视觉消费端只加载基础图 zoom 4。

visual 为 null（仅网格），或以下二选一：

```json
{"kind":"base","image":"localization/maps/map02/4/map02_4.webp",
 "mapping":"localization/maps/map02/4/map02_4_mapping.json"}
```

```json
{"kind":"layer","image":"localization/layers/map02/map02_lv005/scene_map02_lv005_layer_tips_7.webp",
 "catalog_entry":"scene_map02_lv005_layer_tips_7"}
```

grid 是对应 NPZ 路径或 null。图片存在而 grid=null 合法；声明存在也不保证图片或网格存在。
无 visual 且无 grid 的声明留在 levels.json，不需要伪造一条可用资源。
尺寸、bounds、矩阵、origin、cell_size、floor_regions 不在根清单重复维护，以关联文件为准。

根清单只直接列导航文件与定位子清单的摘要；定位子清单再列所有图片/标定/catalog 的摘要。
根中路径统一相对仓库根；定位子清单内部路径相对该 manifest 所在目录，展开后再与 visual 引用比较。
受管文件全集为 navigation_files + localization_manifest 本身 + 其 files；不得重名、漏引用或越出目录。
root dataset 自身、README、schema、工具不放入自身摘要。保持现有 localization manifest 格式，
不再另维护一份重复的根 files 表。禁止绝对路径、盘符、反斜杠和点目录，仍须防止符号链接解析越界。

## 身份与扩区域

普通大地图内共享官方世界 X/Z 的区域使用相同 coordinate_space_id，不随显示名、图片尺寸或文件时间改变。
独立场景使用 `scene:<level_id>` 一类独立身份；归档 map_name=map02 不代表与武陵地表共用坐标。
世界原点、方向或单位真的改变时应引入新的坐标空间和显式迁移，不能仅覆盖旧图让旧标注静默错位。

扩图要区分三个原点：世界坐标定义、图片像素原点、NPZ 数组原点。
世界坐标不变时，后两者可以随扩边调整。相同格子分辨率的网格向西加 L 列、向南加 B 行：

```text
new_origin.x = old_origin.x - L * cell_size
new_origin.z = old_origin.z - B * cell_size
new_cells[i+B,j+L] = old_cells[i,j]
```

新增格子一律为未知。旧 region/links 已是世界坐标，不跟着数组下标移动。
不能把增大数组拉伸成新范围，也不能为绕过大小限制偷偷增大 cell_size。
现有编辑器稠密跨度上限为64,000,000格、已涂格上限300,000；将来超过限制应另行设计
兼容分块与消费端支持，本阶段不更换 NPZ 编码。

底图左侧加 dx、上侧加 dy 像素且内容不缩放，齐次矩阵满足：

```text
p_new = T(dx,dy) * p_old
M_new = M_old * T(-dx,-dy)
inverse_new = T(dx,dy) * inverse_old
```

同时更新 image_size 和控制点像素位置。只在右/下扩边且旧像素位置不变时矩阵可不变，尺寸仍须更新。
有缩放/旋转时使用实际变换的逆矩阵；不要把图片向下的像素行与网格向上的正 Z 混为一谈。
分层沿用官方 bounds；其尺寸/bounds/图片要一起核对。整个图片 bounds 不等于已经绘制的地形覆盖。

## 最小上传集合

| 变化 | 本次 Git 提交至少包含 |
|---|---|
| 通行或阻挡笔画 | 变化的 NPZ + dataset.json；未变化的图片不重复上传 |
| 只改高度区间 | 带该 floor_regions 的 NPZ + dataset.json |
| 主图扩区域 | 图片与mapping成对更新 + 定位manifest + dataset；网格范围也扩时追加相应NPZ |
| 分层图片变化 | 图片 + 同步后的catalog + 定位manifest + dataset |
| 新增层 | 完整levels与相关catalog；实际已有图片/网格才增加对应文件；需要跨层时追加links |
| 连接变化 | links + dataset；所有端点引用的网格必须在完整目标快照中存在 |

首次上传还须包含导出的 .gitattributes。WebP/PNG/NPZ 使用 Git LFS；普通 JSON 使用文本/LF，便于审查，
先固定最终字节再生成清单。不要用 GitHub 网页逐文件上传来替代 LFS 实体上传。

每个提交描述完整快照；Git/LFS 按变化文件传输，不另做手工 patch 格式。
小笔画变化仍会产生一份新的 NPZ 文件/LFS 对象，不应称为逐格差分上传。
ZIP 应从已拉取 LFS 实体的干净快照生成，不能直接把指针文本打包成资源。
提交数据仓后，ok-end-field 仍需更新固定的子模块指针并发布；不会自动追踪数据仓 master。
AnyLabeling 模板仓不负责同步本数据。

## 校验与验收边界

导出器检查原图片与标定尺寸、矩阵有限且互逆、分层目录与图片匹配、网格文件名和meta身份、
连接端点网格/zoom引用、层声明、hash与资源引用闭包。floor_regions 也必须通过保存时同源的严格校验：六个字段齐全、数值有限、区间有序；cells 子矩形维度/数值/区间合法；最多256个区域，每个区域最多50,000个子矩形。坏元数据会拒绝整个导出，不会丢弃或改写后继续发布。先复制输入再生成，交付前复核输入文件集合和摘要；
源数据变化会中止。任务结束前不交付正式目录，非空目标不会被删除或覆盖。

还应使用消费端工具验证定位像素、网格/层/连接语义与真实旧区域控制点。JSON Schema不能检查跨文件引用，
格式校验通过不代表图像一定可识别，也不代表路线可达。新增区域仍须单独做截图回放和实机验证。

编辑器API：POST /api/dataset/export，请求 `{ "target": "D:/nav-grid-upload" }`；
GET /api/dataset/export/status 查询后台任务。目标也可留空或相对data-root，结果返回实际绝对路径。
导出与后台状态恢复已实现；自动commit、push、下载与运行时原子切换未实现，也不由该API触发。
