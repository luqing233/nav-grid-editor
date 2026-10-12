"use strict";

import { CELL_BLOCKED, CELL_FREE, CELL_UNKNOWN, GridDocument } from "./grid/grid-document.js";
import { GridRasterCache } from "./grid/grid-raster-cache.js";
import { loadGridDocument, saveGridDocument } from "./grid/grid-transport.js";
import {
  WALK_POLICIES,
  policyLabel,
  walkableAnomalies as walkableAnomaliesOf,
  walkableComponents as walkableComponentsOf,
} from "./grid/walk-policy.js";
import { pickLayerGroup, tierToBaseMatrix } from "./layers/tier-transform.js";
import { RENDER_LAYERS, RenderScheduler } from "./render/render-scheduler.js";

// ---------------- 状态 ----------------
const TILE = 256;
const state = {
  connected: false,
  service: null,
  follow: true,
  busyFetch: false,
  counts: { new: 0, updated: 0, unchanged: 0, skipped: 0, error: 0 },
  // 每个 (map,zoom) 一个视图：{canvas, g, minX, maxX, minY, maxY, pasted:Set}
  views: new Map(),
  tierCatalogs: new Map(), // "map@zoom" -> 官方分层目录
  activeTier: null,       // {key,id,meta,bitmap}
  tierRequestId: 0,
  tierLoading: false,
  activeKey: null,
  compose: null, // {map, zoom, total, done}
  marks: null,   // 地图标记抓取状态（/api/marks/status）
  position: null, // 官方地图 WS 位置流状态（/api/position/status）
  markers: null, // 当前地图的标记点位（/api/marks/data）
  markHidden: new Set(),  // 被选择器关掉的 templateId（不画这些）
  mpCat: null,      // 选择器当前的大类
  mpCats: [],       // [[大类, [条目]]]，打开选择器时算一次
  markersPromise: null,  // 正在进行的标记请求（并发调用共享它）
  markersKey: null,
  tileMaps: {},
  positionView: { requestId: 0, appliedKey: "" },
  floorMap: { key: "", levelId: "", layers: [], error: null },
  floorOverlap: {
    key: "", xzOnly: new Set(), threeD: new Set(),
    resolved: new Set(), components: [],
  },
  floorPeerGrids: new Map(),
  floorLiveRefreshTimer: 0,
  floorHighlight: { componentId: "", layerId: "" },
  floorMarkedRows: [],
  gridLinks: [],
  layers: {
    base: true,
    tier: true,
    markers: true,
    grid: true,
    selection: true,
    links: true,
    path: true,
  },
  gridOpacity: 0.82,
};

const $ = id => document.getElementById(id);
const statusEl = $("status");
const logBox = $("logBox");
const canvasWrap = $("canvasWrap");
const canvasHolder = $("canvasHolder");
const canvas = $("composeCanvas");
const calibOverlay = $("calibOverlay");
const gridRasterCache = new GridRasterCache();
const gridLayerState = { valid: false, key: "", width: 0, height: 0 };
const gridBoundsEl = $("gridBounds");
const brushCursor = $("brushCursor");
const brushCursorLabel = $("brushCursorLabel");

const MAP_DISPLAY_NAMES = {
  base01: "帝江号",
  map01: "四号谷底",
  map02: "武陵",
};

function mapDisplayName(map) {
  return MAP_DISPLAY_NAMES[map] || map;
}

function setStatus(text, state = "idle") {
  $("statusText").textContent = text;
  statusEl.dataset.state = state;
}

// ---------------- 视图管理 ----------------
function viewKey(map, zoom) { return map + "@" + zoom; }

function hasBounds(v) { return !!v && v.maxX >= v.minX; }
function viewSize(v) { return { w: (v.maxX - v.minX + 1) * TILE, h: (v.maxY - v.minY + 1) * TILE }; }

function getView(map, zoom, bounds) {
  const key = viewKey(map, zoom);
  let v = state.views.get(key);
  if (!v) {
    v = { minX: 0, maxX: -1, minY: 0, maxY: -1, pasted: new Set(),
          overview: null, tierId: null };
  }
  if (bounds) {
    const { minX, maxX, minY, maxY } = bounds;
    if (!hasBounds(v)) {
      v.minX = minX; v.maxX = maxX; v.minY = minY; v.maxY = maxY;
    } else {
      v.minX = Math.min(v.minX, minX); v.maxX = Math.max(v.maxX, maxX);
      v.minY = Math.min(v.minY, minY); v.maxY = Math.max(v.maxY, maxY);
    }
  }
  state.views.set(key, v);
  return v;
}

// 只把瓦片坐标并进视图范围；像素等到它进入可见区域再取。
function growForTile(map, zoom, x, y) {
  return getView(map, zoom, { minX: x, maxX: x, minY: y, maxY: y });
}

// 瓦片到达：原来这里要 fetch + 解码 + 画进"整张地图尺寸"的画布，一趟抓取
// 跑几千次；现在只记账，渲染时按可见范围取图。
function noteTile(map, zoom, x, y, kind) {
  const key = viewKey(map, zoom);
  const before = state.views.get(key);
  const oldBounds = hasBounds(before)
    ? [before.minX, before.maxX, before.minY, before.maxY].join(",")
    : "";
  const v = growForTile(map, zoom, x, y);
  const newBounds = [v.minX, v.maxX, v.minY, v.maxY].join(",");
  const tKey = x + "," + y;
  if (v.pasted.has(tKey)) return;
  v.pasted.add(tKey);
  if (key === state.activeKey) {
    updateCurBar();
    requestRender(oldBounds === newBounds ? RENDER_LAYERS.BASE : RENDER_LAYERS.ALL);
    if (kind === "new") flashTile(map, zoom, x, y);
  }
}

function setActive(key) {
  if (state.activeKey === key) return;
  const previousKey = state.activeKey;
  if (state.activeTier && state.activeTier.key !== key) {
    clearActiveTier(false, previousKey);
  }
  if (link.drag) {
    link.drag = null;
    requestRender(RENDER_LAYERS.LINK);
  }
  clearTileCache();
  for (const [viewKeyValue, view] of state.views) {
    if (viewKeyValue === key || !view.overview) continue;
    if (typeof view.overview.close === "function") view.overview.close();
    view.overview = null;
  }
  state.activeKey = key;
  redrawActive();
  fitView();
  updateCurBar();
  const [activeMap] = key.split("@");
  setStatus("当前视图: " + mapDisplayName(activeMap) +
            "（拖动平移 · 滚轮缩放 · 双击适应）", "ready");
  loadMappingForCurrent();
  void loadTierCatalog();
  syncTierControls();
  updatePlayerMarker(null);
  hidePickMarker();
  $("pickReadout").textContent = "-";
  if (edit.mode || link.mode) loadGrid2D(false);
  loadMarkers();   // 换了图，标记也要跟着换
  void loadGridLinks();
}

// 顶部信息条：实时显示当前拼接的地图名 / zoom / 瓦片数 / 坐标范围
function updateCurBar() {
  const bar = (id, txt) => { $(id).textContent = txt; };
  if (!state.activeKey) {
    bar("curMap", "-"); bar("curZoom", "-"); bar("curCount", "0");
    bar("curX", "-"); bar("curY", "-"); bar("canvasViewLabel", "NO MAP");
    bar("sideViewLabel", "-");
    return;
  }
  const [map, zoom] = state.activeKey.split("@");
  const displayName = mapDisplayName(map);
  bar("curMap", displayName);
  bar("curZoom", zoom);
  bar("canvasViewLabel", displayName + " / L" + zoom);
  bar("sideViewLabel", displayName + " / L" + zoom);
  const v = state.views.get(state.activeKey);
  if (hasBounds(v)) {
    bar("curCount", v.pasted.size);
    bar("curX", v.minX + " ~ " + v.maxX);
    bar("curY", v.minY + " ~ " + v.maxY);
  } else {
    bar("curCount", "0"); bar("curX", "-"); bar("curY", "-");
  }
}

function redrawActive() {
  applyView();
  const v = state.views.get(state.activeKey);
  $("cPasted").textContent = hasBounds(v) ? v.pasted.size : 0;
}

// =========================================================
// 视口渲染
// =========================================================
// 画布只覆盖"当前看得见的那块地图"，尺寸跟着窗口走，不跟地图走。
//
// 以前是按整张图的像素尺寸建画布：map02@4 是 10752x15872（1.7 亿像素），
// 单个 canvas 的位图就约 685MB，路径层还要再开一张同样大的；一旦超过
// GPU 纹理上限，浏览器会悄悄退回软件渲染，于是"打开就卡、拖动不跟手"。
// 现在瓦片按需取、只画可见范围，画布永远只有一屏大小。
const mapView = { scale: 1, panX: 0, panY: 0,
  // 焦点：用户"正在看"的那个点（holder 坐标）。滚轮缩放时记成鼠标锚点——
  // 你朝哪儿放大，那一块就优先加载；拖动平移之后作废、退回视口中心。
  // 瓦片按到它的距离排序后再发请求，见 render()。
  focus: null };
let dragState = null;
let viewFrame = 0;
let viewFrameNeedsRender = false;
let renderOverscan = 0;
let dragBufferRect = null;
let isPanning = false;
let deferredRenderLayers = 0;
let brushCursorMinPx = 7;

// 拖动时让画布多覆盖视口四周一段距离。CSS transform 只负责移动已经画好的
// 合成层，主线程不必每帧重画；越界前再重建一次缓冲。
const DRAG_OVERSCAN = 0.36;
const DRAG_BUFFER_EDGE = 0.08;

function refreshUiMetrics() {
  const rootStyle = getComputedStyle(document.documentElement);
  const value = parseFloat(rootStyle.getPropertyValue("--ui-scale"));
  const scale = Number.isFinite(value) && value > 0 ? value : 1;
  const configured = parseFloat(rootStyle.getPropertyValue("--brush-cursor-min"));
  const base = Number.isFinite(configured) && configured > 0 ? configured : 7;
  brushCursorMinPx = base * scale;
}

function focusHolder() {
  if (mapView.focus) return mapView.focus;
  const vis = visibleRect();   // 没设焦点就用视口中心
  return { x: vis.x + vis.w / 2, y: vis.y + vis.h / 2 };
}

const DPR = Math.max(1, Math.min(1.5, window.devicePixelRatio || 1));
// 解码后的瓦片位图上限（256×256×4B = 256KiB/张，1200 张约 300MiB）。
// **必须明显大于 MAX_TILES_PER_FRAME**：缓存装不下"一屏最多会画的瓦片数"时，
// 同一屏里的瓦片会互相把对方挤出 LRU，于是每一帧都重新请求自己，
// 请求量成倍膨胀（实测 1100×749 窗口、一屏 580 张时，580 个坐标被取了 1864 次）。
const TILE_CACHE_MAX = 420;
const TILE_CACHE_MAX_BYTES = 96 * 1024 * 1024;
const MAX_TILES_PER_FRAME = 320; // 视野内瓦片过多时只画缩略图，不逐张请求
const TILE_DETAIL_MIN_SCALE = 0.16;
const TIER_PREVIEW_MAX = 2048;
const TIER_FULL_MIN_SCALE = 0.32;
const TIER_FULL_RELEASE_SCALE = 0.22;
const tileCache = new Map();     // "map/zoom/x_y" -> ImageBitmap | "loading" | {missing:true}
let tileCacheBytes = 0;
let tileCacheGeneration = 0;
const renderScheduler = new RenderScheduler((layers) => render(layers));

function requestRender(layers = RENDER_LAYERS.ALL) {
  if (isPanning) {
    deferredRenderLayers |= layers;
    return;
  }
  renderScheduler.request(layers);
}

// 可见区域（holder 坐标系：1 单位 = 总图像素）
function visibleRect() {
  const s = mapView.scale;
  const w = canvasWrap.clientWidth / s;
  const h = canvasWrap.clientHeight / s;
  const ox = w * renderOverscan;
  const oy = h * renderOverscan;
  return { x: -mapView.panX / s - ox, y: -mapView.panY / s - oy,
           w: w + ox * 2, h: h + oy * 2 };
}

// 把一层画布摆到可见区域上方，并把它的 2D 上下文设成"直接按 holder 坐标作图"。
// 画布 CSS 尺寸用 holder 单位，backing store 用设备像素，整体缩放交给父层 transform。
function layoutLayer(el, g, vis, s) {
  el.style.left = vis.x + "px";
  el.style.top = vis.y + "px";
  el.style.width = vis.w + "px";
  el.style.height = vis.h + "px";
  const bw = Math.max(1, Math.round(vis.w * s * DPR));
  const bh = Math.max(1, Math.round(vis.h * s * DPR));
  if (el.width !== bw) el.width = bw;
  if (el.height !== bh) el.height = bh;
  const kx = bw / vis.w, ky = bh / vis.h;
  g.setTransform(kx, 0, 0, ky, -vis.x * kx, -vis.y * ky);
  return g;
}

// LRU：命中后重新插入，超限时把最旧的位图 close 掉释放显存
function cacheGet(key) {
  if (!tileCache.has(key)) return undefined;
  const v = tileCache.get(key);
  tileCache.delete(key);
  tileCache.set(key, v);
  return v;
}
function bitmapBytes(value) {
  return isBitmap(value)
    ? (value.width || 0) * (value.height || 0) * 4
    : 0;
}
function cachePut(key, val) {
  if (tileCache.has(key)) {
    tileCacheBytes -= bitmapBytes(tileCache.get(key));
    tileCache.delete(key);
  }
  tileCacheBytes += bitmapBytes(val);
  tileCache.set(key, val);
  while (tileCache.size > TILE_CACHE_MAX ||
         tileCacheBytes > TILE_CACHE_MAX_BYTES) {
    const oldest = tileCache.keys().next().value;
    const old = tileCache.get(oldest);
    tileCacheBytes -= bitmapBytes(old);
    if (old && typeof old.close === "function") old.close();
    tileCache.delete(oldest);
  }
}
function clearTileCache() {
  tileCacheGeneration++;
  for (const value of tileCache.values()) {
    if (value && typeof value.close === "function") value.close();
  }
  tileCache.clear();
  tileCacheBytes = 0;
}

// 只有解码好的位图能画
function isBitmap(v) {
  return !!v && v !== "loading" && !v.missing;
}
function isMissing(v) {
  return !!v && v.missing === true;
}

// 404 的瓦片要记住"这一轮没有"，但不能靠定时重试：可见范围里可能有几百个
// 空洞，每 5 秒重发一轮就是每秒上百个请求，浏览器直接 ERR_INSUFFICIENT_RESOURCES。
// 改成**事件驱动**——服务端什么时候真有了这张瓦片，就什么时候作废这个标记。
const TILE_KEY = (map, zoom, x, y) => map + "/" + zoom + "/" + x + "_" + y;

// 单张：SSE tile 事件说 map/zoom/x/y 有了，只作废那一格
function forgetMissingTile(map, zoom, x, y) {
  const k = TILE_KEY(map, zoom, x, y);
  if (isMissing(tileCache.get(k))) {
    tileCache.delete(k);
    requestRender(RENDER_LAYERS.BASE);
  }
}

// 批量：抓取结束 / 合并到 latest 之后，整批瓦片可能都变了，一次性作废
function forgetAllMissing() {
  let hit = false;
  for (const [k, v] of [...tileCache]) {
    if (isMissing(v)) { tileCache.delete(k); hit = true; }
  }
  if (hit) requestRender(RENDER_LAYERS.BASE);
}

function requestTileBitmap(map, zoom, x, y) {
  const key = TILE_KEY(map, zoom, x, y);
  const cur = tileCache.get(key);
  // 位图 / 正在下载 / 已确认这次没有 -> 都不重复发请求
  if (isBitmap(cur) || cur === "loading" || isMissing(cur)) return;
  cachePut(key, "loading");
  const generation = tileCacheGeneration;
  fetch("/tiles/" + map + "/" + zoom + "/" + x + "_" + y + ".png")
    .then(r => r.ok ? r.blob() : Promise.reject(new Error("HTTP " + r.status)))
    .then(blob => window.createImageBitmap ? createImageBitmap(blob) : loadImageFromBlob(blob))
    .then(bmp => {
      if (generation !== tileCacheGeneration) {
        if (bmp && typeof bmp.close === "function") bmp.close();
        return;
      }
      cachePut(key, bmp);
      if (state.activeKey === viewKey(map, zoom)) requestRender(RENDER_LAYERS.BASE);
    })
    .catch(() => {
      if (generation === tileCacheGeneration) cachePut(key, { missing: true });
    });
}

async function loadOverview(map, zoom) {
  const key = viewKey(map, zoom);
  const v0 = state.views.get(key);
  if (!hasBounds(v0) || v0.overview) return;
  const generation = tileCacheGeneration;
  try {
    const resp = await fetch("/api/overview?map=" + encodeURIComponent(map) +
                             "&zoom=" + encodeURIComponent(zoom));
    if (!resp.ok) return;
    const blob = await resp.blob();
    const bmp = window.createImageBitmap ? await createImageBitmap(blob)
                                         : await loadImageFromBlob(blob);
    if (generation !== tileCacheGeneration) {
      if (bmp && typeof bmp.close === "function") bmp.close();
      return;
    }
    const v = state.views.get(key);
    if (!v) return;
    v.overview = bmp;
    if (state.activeKey === key) requestRender(RENDER_LAYERS.BASE);
  } catch (e) {
    addLog("缩略总图加载失败: " + e.message, "t-warn");
  }
}

function drawTierOverlay(g, vis) {
  const tier = state.activeTier;
  if (!tier || tier.key !== state.activeKey || !state.layers.tier) return;
  if (!calib.mapping || calib.key !== state.activeKey) return;

  let matrix;
  try {
    matrix = tierToBaseMatrix(tier.meta, calib.mapping.inverse_matrix);
  } catch (e) {
    addLog("分层变换失败: " + e.message, "t-err");
    return;
  }

  // 官方页面也使用同样的“压暗底图，只照亮当前层”方式。
  g.save();
  g.globalAlpha = 0.60;
  g.fillStyle = "#000";
  g.fillRect(vis.x, vis.y, vis.w, vis.h);
  g.restore();

  g.save();
  g.imageSmoothingEnabled = true;
  g.transform(matrix[0], matrix[1], matrix[2],
              matrix[3], matrix[4], matrix[5]);
  const bitmap = tier.bitmap || tier.preview;
  if (!bitmap) {
    g.restore();
    return;
  }
  g.drawImage(bitmap, 0, 0, tier.meta.width, tier.meta.height);
  g.restore();
}

function render(layers = RENDER_LAYERS.ALL) {
  const s = mapView.scale;
  const vis = visibleRect();
  dragBufferRect = { x: vis.x, y: vis.y, w: vis.w, h: vis.h };
  const v = state.views.get(state.activeKey);

  if (layers & RENDER_LAYERS.BASE) {
    const g = layoutLayer(canvas, canvas.getContext("2d"), vis, s);
    g.fillStyle = "#080808";
    g.fillRect(vis.x, vis.y, vis.w, vis.h);

    if (state.layers.base && hasBounds(v)) {
      const size = viewSize(v);
      const useTiles = s >= TILE_DETAIL_MIN_SCALE;
      // 1) 缩略总图打底：秒出图，缩着看时不用几千张瓦片
      if (v.overview && !useTiles) {
        g.imageSmoothingEnabled = true;
        g.drawImage(v.overview, 0, 0, size.w, size.h);
      }
      // 2) 瓦片层：只画可见范围，缺图的按需取，取回后自动重画
      // 可见矩形是 holder 坐标（(瓦片- minX)*TILE），要加上 minX 才是瓦片下标
      const x0 = Math.max(v.minX, Math.floor(vis.x / TILE) + v.minX);
      const y0 = Math.max(v.minY, Math.floor(vis.y / TILE) + v.minY);
      const x1 = Math.min(v.maxX, Math.ceil((vis.x + vis.w) / TILE) - 1 + v.minX);
      const y1 = Math.min(v.maxY, Math.ceil((vis.y + vis.h) / TILE) - 1 + v.minY);
      const need = Math.max(0, x1 - x0 + 1) * Math.max(0, y1 - y0 + 1);
      let drewTile = false;
      if (useTiles && need > 0 && need <= MAX_TILES_PER_FRAME) {
        const [map, zoom] = state.activeKey.split("@");
        const pending = [];
        for (let y = y0; y <= y1; y++) {
          for (let x = x0; x <= x1; x++) {
            const key = TILE_KEY(map, zoom, x, y);
            const bmp = cacheGet(key);
            if (isBitmap(bmp)) {
              g.drawImage(bmp, (x - v.minX) * TILE, (y - v.minY) * TILE, TILE, TILE);
              drewTile = true;
            } else if (bmp !== "loading") {
              pending.push([x, y]);
            }
          }
        }
        // 按到"焦点"的距离排序后再发请求。浏览器对同一主机的并发约 6 路、且
        // 基本按发出顺序服务，所以先发≈先到；排完序就等于"我盯着的那块先清晰"。
        // 只对还没发出过的格子排序（发出去的会被标成 loading，下一帧就不在这里了），
        // 所以不会每帧重复排。
        if (pending.length) {
          const f = focusHolder();
          pending.sort((a, b) => {
            const ax = (a[0] - v.minX + 0.5) * TILE - f.x;
            const ay = (a[1] - v.minY + 0.5) * TILE - f.y;
            const bx = (b[0] - v.minX + 0.5) * TILE - f.x;
            const by = (b[1] - v.minY + 0.5) * TILE - f.y;
            return (ax * ax + ay * ay) - (bx * bx + by * by);
          });
          for (const [x, y] of pending) requestTileBitmap(map, zoom, x, y);
        }
        // 瓦片接缝
        g.strokeStyle = "rgba(150,165,185,0.045)";
        g.lineWidth = 1 / s;
        g.beginPath();
        for (let x = x0; x <= x1 + 1; x++) {
          const px = (x - v.minX) * TILE;
          g.moveTo(px, (y0 - v.minY) * TILE);
          g.lineTo(px, (y1 - v.minY + 1) * TILE);
        }
        for (let y = y0; y <= y1 + 1; y++) {
          const py = (y - v.minY) * TILE;
          g.moveTo((x0 - v.minX) * TILE, py);
          g.lineTo((x1 - v.minX + 1) * TILE, py);
        }
        g.stroke();
      }
      if (v.overview && !drewTile) {
        g.imageSmoothingEnabled = true;
        g.drawImage(v.overview, 0, 0, size.w, size.h);
      }
    }

    // 2.1) 官方分层图片：底图之上、标记/网格/路径之下
    drawTierOverlay(g, vis);

    // 2.5) 地图标记层：画在瓦片之上、网格/路径之下
    if (state.layers.markers) drawMarkers(g);
  }

  // 3) 网格层在编辑和连接模式都显示；选择层仍然只属于编辑模式。
  if ((edit.mode || link.mode) && (layers & RENDER_LAYERS.GRID)) {
    const ge = layoutLayer(editCanvasEl, ectx, vis, s);
    if (state.layers.grid) drawAllCells(ge);
    else {
      ge.clearRect(vis.x, vis.y, vis.w, vis.h);
      gridBoundsEl.style.display = "none";
    }
  }
  if ((edit.mode || link.mode) && (layers & RENDER_LAYERS.SELECTION)) {
    const gs = layoutLayer(selCanvasEl, sctx, vis, s);
    if (state.layers.selection) drawSelLayer(gs);
    else gs.clearRect(vis.x, vis.y, vis.w, vis.h);
  }
  if (layers & RENDER_LAYERS.LINK) {
    const gl = layoutLayer(linkCanvasEl, lctx, vis, s);
    if (state.layers.links) drawLinkLayer(gl);
    else gl.clearRect(vis.x, vis.y, vis.w, vis.h);
  }
  // 4) 路径层
  if (layers & RENDER_LAYERS.PATH) {
    const gp = layoutLayer(pathCanvasEl, pctx, vis, s);
    if (state.layers.path) drawPath(gp);
    else gp.clearRect(vis.x, vis.y, vis.w, vis.h);
  }
}

function applyViewTransform() {
  canvasHolder.style.transform =
    "translate3d(" + mapView.panX + "px," + mapView.panY + "px,0) scale(" + mapView.scale + ")";
}

function applyViewChrome() {
  applyViewTransform();
  if (calib.mode) renderCalibMarks();
  updatePlayerMarker(lastPos);
  if (edit.mode && edit.cursorCell) updateBrushCursor(edit.cursorCell[0], edit.cursorCell[1]);
}

function applyView(renderNow = false, renderDeferred = true) {
  applyViewChrome();
  if (renderNow || !isPanning) syncTierResolution();
  if (renderNow) {
    deferredRenderLayers = 0;
    render(RENDER_LAYERS.ALL);
  } else if (renderDeferred && !isPanning) {
    requestRender();
  }
}

function scheduleViewFrame(renderNow = false) {
  viewFrameNeedsRender = viewFrameNeedsRender || renderNow;
  if (viewFrame) return;
  viewFrame = requestAnimationFrame(() => {
    const needsRender = viewFrameNeedsRender;
    viewFrame = 0;
    viewFrameNeedsRender = false;
    applyView(needsRender, false);
  });
}

function resetViewFrame() {
  if (viewFrame) cancelAnimationFrame(viewFrame);
  viewFrame = 0;
  viewFrameNeedsRender = false;
}

function currentViewportRect() {
  const s = mapView.scale;
  return {
    x: -mapView.panX / s,
    y: -mapView.panY / s,
    w: canvasWrap.clientWidth / s,
    h: canvasWrap.clientHeight / s,
  };
}

function viewportInsideDragBuffer() {
  if (!dragBufferRect || renderOverscan <= 0) return false;
  const view = currentViewportRect();
  const padX = view.w * DRAG_BUFFER_EDGE;
  const padY = view.h * DRAG_BUFFER_EDGE;
  return view.x >= dragBufferRect.x + padX &&
         view.y >= dragBufferRect.y + padY &&
         view.x + view.w <= dragBufferRect.x + dragBufferRect.w - padX &&
         view.y + view.h <= dragBufferRect.y + dragBufferRect.h - padY;
}

function startPanGesture(e, hideBrush = false) {
  dragState = {
    id: e.pointerId,
    sx: e.clientX,
    sy: e.clientY,
    px: mapView.panX,
    py: mapView.panY,
    moved: false,
  };
  canvasWrap.setPointerCapture(e.pointerId);
  canvasWrap.classList.add("dragging");
  document.body.classList.add("map-panning");
  if (hideBrush) hideBrushCursor();
}

function activatePanBuffer() {
  if (isPanning) return;
  isPanning = true;
  renderOverscan = DRAG_OVERSCAN;
  deferredRenderLayers = 0;
  renderScheduler.cancel();
  resetViewFrame();
  applyView(true);
}

function finishPanGesture() {
  if (!isPanning) return;
  isPanning = false;
  renderOverscan = 0;
  deferredRenderLayers = 0;
  renderScheduler.cancel();
  resetViewFrame();
  applyView(true);
}

// 地图小于窗口时居中，大于窗口时限制拖出边界
function clampPan() {
  const v = state.views.get(state.activeKey);
  const size = hasBounds(v) ? viewSize(v) : { w: 0, h: 0 };
  const w = size.w * mapView.scale, h = size.h * mapView.scale;
  const vw = canvasWrap.clientWidth, vh = canvasWrap.clientHeight;
  if (w <= vw) mapView.panX = (vw - w) / 2;
  else mapView.panX = Math.min(0, Math.max(vw - w, mapView.panX));
  if (h <= vh) mapView.panY = (vh - h) / 2;
  else mapView.panY = Math.min(0, Math.max(vh - h, mapView.panY));
}

// 缩放并居中显示整张总图
function fitView() {
  const v = state.views.get(state.activeKey);
  if (!hasBounds(v)) return;
  const size = viewSize(v);
  const wrapW = Math.max(32, canvasWrap.clientWidth - 32);
  const wrapH = Math.max(32, canvasWrap.clientHeight - 32);
  mapView.scale = Math.max(0.02, Math.min(1, wrapW / size.w, wrapH / size.h));
  mapView.focus = null;   // 整图铺满，焦点交给视口中心
  clampPan();
  applyView(true);
}

// 以鼠标位置为中心缩放
function zoomAt(cx, cy, factor) {
  showMarkTip(null);   // 缩放后徽章位置变了，旧提示会指错地方
  const old = mapView.scale;
  // 先记下鼠标锚点所在的 holder 坐标：缩放前后这个点是不动的，它就是我们
  // 要"优先加载"的那个焦点。cx/cy 是相对 canvasWrap 的屏幕坐标。
  mapView.focus = { x: (cx - mapView.panX) / old, y: (cy - mapView.panY) / old };
  mapView.scale = Math.min(8, Math.max(0.02, old * factor));
  const k = mapView.scale / old;
  mapView.panX = cx - (cx - mapView.panX) * k;
  mapView.panY = cy - (cy - mapView.panY) * k;
  clampPan();
  applyView(true);
}

function clearView(map, zoom) {
  state.views.delete(viewKey(map, zoom));
  mapView.focus = null;   // 换了张图，之前的焦点没有意义了
  if (state.activeKey === viewKey(map, zoom)) {
    state.activeKey = null;
    applyView();
  }
  $("cPasted").textContent = 0;
  updateCurBar();
}

// ---------------- 单张瓦片闪一下（只对可见瓦片建 DOM） ----------------
function flashTile(map, zoom, x, y) {
  const v = state.views.get(viewKey(map, zoom));
  if (!hasBounds(v) || viewKey(map, zoom) !== state.activeKey) return;
  const vis = visibleRect();
  const px = (x - v.minX) * TILE, py = (y - v.minY) * TILE;
  if (px + TILE < vis.x || px > vis.x + vis.w) return;
  if (py + TILE < vis.y || py > vis.y + vis.h) return;
  // 闪框与画布同处 canvasHolder 内，随平移/缩放一起变换，直接用瓦片内部像素定位
  const d = document.createElement("div");
  d.className = "tileFlash";
  d.style.left = px + "px";
  d.style.top = py + "px";
  d.style.width = (TILE - 2) + "px";
  d.style.height = (TILE - 2) + "px";
  canvasHolder.appendChild(d);
  setTimeout(() => { d.style.opacity = "0"; }, 60);
  setTimeout(() => d.remove(), 1100);
}

async function downloadTile(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error("HTTP " + resp.status);
  return resp.blob();
}

// blob → Image（内部自动管理 objectURL 生命周期）
function loadImageFromBlob(blob) {
  return new Promise((res, rej) => {
    const url = URL.createObjectURL(blob);
    const img = new Image();
    img.onload = () => { res(img); setTimeout(() => URL.revokeObjectURL(url), 1000); };
    img.onerror = () => { URL.revokeObjectURL(url); rej(new Error("图片解码失败")); };
    img.src = url;
  });
}

// ---------------- 地图标定（建立像素↔游戏坐标坐标系）与玩家定位 ----------------
const calib = {
  mode: false,      // 标定模式开关
  points: [],       // {pixel:[px,py], world:[x,z], enabled, error, inlier}
  mapping: null,    // 已加载/已保存的 mapping {matrix, inverse_matrix, ...}
  key: null,        // mapping 属于哪个视图 map@zoom
  pendingPixel: null,
};

const calibHistory = { undo: [], redo: [] };

function calibSnapshot() {
  return {
    points: calib.points.map(p => ({
      pixel: [p.pixel[0], p.pixel[1]],
      world: p.world ? [p.world[0], p.world[1]] : null,
      enabled: !!p.enabled,
      error: p.error,
      inlier: p.inlier,
    })),
    mapping: calib.mapping ? JSON.parse(JSON.stringify(calib.mapping)) : null,
  };
}

function pushCalibHistory() {
  calibHistory.undo.push(calibSnapshot());
  if (calibHistory.undo.length > 100) calibHistory.undo.shift();
  calibHistory.redo.length = 0;
  syncHistoryButtons();
}

function applyCalibSnapshot(snapshot) {
  calib.points = snapshot.points.map(p => ({
    pixel: [p.pixel[0], p.pixel[1]],
    world: p.world ? [p.world[0], p.world[1]] : null,
    enabled: !!p.enabled,
    error: p.error,
    inlier: p.inlier,
  }));
  calib.mapping = snapshot.mapping ? JSON.parse(JSON.stringify(snapshot.mapping)) : null;
  renderCalibList();
  renderCalibMarks();
  updatePlayerMarker(lastPos);
  requestRender();
}

function undoCalibEdit() {
  const snapshot = calibHistory.undo.pop();
  if (!snapshot) {
    setStatus("没有可撤销的标定操作", "idle");
    return false;
  }
  calibHistory.redo.push(calibSnapshot());
  applyCalibSnapshot(snapshot);
  syncHistoryButtons();
  setStatus("已撤销标定操作", "ready");
  return true;
}

function redoCalibEdit() {
  const snapshot = calibHistory.redo.pop();
  if (!snapshot) {
    setStatus("没有可重做的标定操作", "idle");
    return false;
  }
  calibHistory.undo.push(calibSnapshot());
  applyCalibSnapshot(snapshot);
  syncHistoryButtons();
  setStatus("已重做标定操作", "ready");
  return true;
}

function renderCalibMarks() {
  calibOverlay.replaceChildren();
  // 定位点**只在标定模式显示**：浏览/网格/取坐标时它们会压在地图上挡视线
  // （标定时它们才是"我点了哪几个点"的参照）
  if (!calib.mode) return;
  if (!calib.key || calib.key !== state.activeKey) return;
  calib.points.forEach((p, i) => {
    const [px, py] = p.pixel;
    const point = document.createElement("div");
    point.className = "calibPoint" + (p.enabled ? "" : " is-disabled");
    point.style.left = (px * mapView.scale + mapView.panX) + "px";
    point.style.top = (py * mapView.scale + mapView.panY) + "px";
    const d = document.createElement("div");
    d.className = "calibMark";
    const n = document.createElement("div");
    n.className = "calibNum";
    n.textContent = "#" + (i + 1) + (p.world ? " (" + p.world[0].toFixed(1) + "," + p.world[1].toFixed(1) + ")" : "");
    point.append(d, n);
    calibOverlay.appendChild(point);
  });
}

function renderCalibList() {
  const box = $("calibList");
  box.innerHTML = "";
  calib.points.forEach((p, i) => {
    const row = document.createElement("div");
    row.className = "calibRow";
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.checked = !!p.enabled;
    cb.onchange = () => {
      pushCalibHistory();
      p.enabled = cb.checked;
      renderCalibMarks();
      updateCalibSaveBtn();
    };
    const info = document.createElement("span");
    info.textContent = "#" + (i + 1) +
      " px(" + Math.round(p.pixel[0]) + "," + Math.round(p.pixel[1]) + ")" +
      (p.world ? " → (" + p.world[0].toFixed(1) + "," + p.world[1].toFixed(1) + ")" : "") +
      (p.error != null ? " err " + p.error.toFixed(2) + (p.inlier === false ? " 离群" : "") : "");
    const del = document.createElement("button");
    del.className = "c-del"; del.textContent = "✕";
    del.onclick = () => {
      pushCalibHistory();
      calib.points.splice(i, 1);
      renderCalibList();
      renderCalibMarks();
      updateCalibSaveBtn();
    };
    row.appendChild(cb); row.appendChild(info); row.appendChild(del);
    box.appendChild(row);
  });
  updateCalibSaveBtn();
}

function updateCalibSaveBtn() {
  $("btnCalibSave").disabled = calib.points.filter(p => p.enabled).length < 3;
  syncHistoryButtons();
}

// ---------------- 模式：浏览 / 标定 / 网格 / 取坐标 ----------------
// 四选一，而不是三个各自开关的按钮。
// 不引入 state.mode 这种会和既有标志漂移的新状态：模式一律从既有标志**派生**，
// 所以指示器永远反映真实状态——比如未标定时进网格会被拒，指示器不会骗人。
const MODES = {
  browse: { label: "浏览",   color: "#c3fd43" },
  calib:  { label: "标定",   color: "#c3fd43" },
  edit:   { label: "网格",   color: "#c3fd43" },
  link:   { label: "连接",   color: "#c3fd43" },
  pick:   { label: "取坐标", color: "#c3fd43" },
};
const DEFAULT_SIDE_TAB = {
  browse: "capture", calib: "calib", edit: "grid",
  link: "grid", pick: "coords",
};
const MODE_BY_SIDE_TAB = { capture: "browse", calib: "calib", grid: "edit", coords: "pick" };
let activeSideTab = null;

function currentMode() {
  if (edit.mode) return "edit";
  if (link.mode) return "link";
  if (calib.mode) return "calib";
  if (pick.mode) return "pick";
  return "browse";
}

// 按当前模式重画所有"模式相关"的部分：模式段选中态、行2 的动作组、侧栏面板、
// 状态栏模式标签，以及那个统一的 --mode 颜色变量（行1 强调条 / 选中态 / 标签共用）。
function syncModeChrome() {
  const m = currentMode();
  const modeOrder = {
    browse: "01", calib: "02", edit: "03", link: "04", pick: "05",
  };
  document.documentElement.style.setProperty("--mode", MODES[m].color);
  document.body.dataset.mode = m;

  ["browse", "calib", "edit", "link", "pick"].forEach(k => {
    const btn = $({
      browse: "btnBrowse", calib: "btnCalib", edit: "btnEdit",
      link: "btnLink", pick: "btnPick",
    }[k]);
    const active = k === m;
    btn.classList.toggle("on", active);
    btn.setAttribute("aria-pressed", String(active));
  });
  // 上下文工具组仍按模式显示；侧栏由 Tab 管理，不再把所有面板纵向堆在一起。
  const inMode = el => (el.dataset.mode || "").split(/\s+/).includes(m);
  document.querySelectorAll(".tb-act").forEach(el => { el.hidden = !inMode(el); });
  const tabMode = activeSideTab && MODE_BY_SIDE_TAB[activeSideTab];
  if (!activeSideTab || (tabMode && tabMode !== m)) {
    setSideTab(DEFAULT_SIDE_TAB[m], { syncMode: false });
  }
  $("modeTag").textContent = MODES[m].label;
  $("canvasModeLabel").textContent = MODES[m].label;
  $("sideModeCode").textContent = modeOrder[m];
  $("sideModeLabel").textContent = MODES[m].label;
  document.querySelectorAll(".rail-mode").forEach(btn => {
    btn.classList.toggle("is-active", btn.dataset.railMode === m);
  });
  syncHistoryButtons();
  requestRender();   // 编辑层只在 edit 模式画，切换后要重画
}

function setSidePanel(open) {
  document.body.classList.toggle("side-open", open);
  document.body.classList.toggle("side-collapsed", !open);
  document.body.classList.remove("side-closed");
  const toggle = $("btnSideToggle");
  toggle.setAttribute("aria-label", open ? "收起检查器" : "展开检查器");
  toggle.title = open ? "收起检查器" : "展开检查器";
  toggle.setAttribute("aria-expanded", String(open));
  if (open && !activeSideTab) setSideTab(DEFAULT_SIDE_TAB[currentMode()]);
}

function isSidePanelOpen() {
  return !document.body.classList.contains("side-collapsed");
}

function setSideTab(name, { syncMode = true } = {}) {
  let target = name;
  const mode = MODE_BY_SIDE_TAB[name];
  if (syncMode && mode && currentMode() !== mode) {
    setMode(mode);
    if (currentMode() !== mode) target = DEFAULT_SIDE_TAB[currentMode()];
  }
  activeSideTab = target;
  document.querySelectorAll("[data-side-tab]").forEach(button => {
    const active = button.dataset.sideTab === target;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", String(active));
  });
  document.querySelectorAll(".side-panel").forEach(panel => {
    panel.hidden = panel.dataset.panel !== target;
  });
}

function setMode(name) {
  // 关掉除目标外的模式，一律委托给各自的 setter，保留它们的清理副作用
  // （setEditMode(false) 要清画笔/框选/选区并归零覆盖画布）。
  if (name !== "edit") setEditMode(false);
  if (name !== "link") setLinkMode(false);
  if (name !== "calib") setCalibMode(false);
  if (name !== "pick") setPickMode(false);

  if (name === "calib") setCalibMode(true);
  else if (name === "edit") setEditMode(true);   // 未标定时会被拒绝
  else if (name === "link") setLinkMode(true);
  else if (name === "pick") setPickMode(true);

  // 不假设切换成功：setEditMode 可能因未标定而拒绝，所以最后按真实状态统一重画
  syncModeChrome();
}

function setCalibMode(on) {
  if (on) {
    if (pick.mode) setPickMode(false);
    if (link.mode) setLinkMode(false);
    // 以前这里漏了 edit：网格模式下点标定会让两个模式同时亮着（只是被鼠标分发
    // 的 !edit.mode 挡住，平时看不出来）。单选组要求这里必须对称。
    if (edit.mode) setEditMode(false);
  }
  calib.mode = on;
  canvasWrap.style.cursor = on ? "crosshair" : "";
  setStatus(on
    ? "标定模式：点击地图上的特征点 → 输入游戏坐标 X/Z；≥3 个点后点“计算并保存标定”"
    : "已退出标定模式", on ? "active" : "idle");
  renderCalibMarks();   // 定位点只在标定模式显示，进出都要重画
  syncModeChrome();
}

// 点击位置 → holder 坐标（即总图像素）。画布只覆盖可见区域，
// 所以这里直接用 view 变换反算，不再依赖画布尺寸。
let gestureRect = null;

function canvasPixelOfClient(clientX, clientY, rect = gestureRect || canvasWrap.getBoundingClientRect()) {
  if (!rect.width || !rect.height) return null;
  return [(clientX - rect.left - mapView.panX) / mapView.scale,
          (clientY - rect.top - mapView.panY) / mapView.scale];
}

function canvasPixelOfEvent(e) {
  return canvasPixelOfClient(e.clientX, e.clientY);
}

function openCalibDialog(px, py) {
  calib.pendingPixel = [px, py];
  $("calibPx").textContent = Math.round(px) + ", " + Math.round(py);
  $("calibX").value = ""; $("calibZ").value = "";
  $("calibDlg").style.display = "block";
  $("calibX").focus();
}

function closeCalibDialog() {
  $("calibDlg").style.display = "none";
  calib.pendingPixel = null;
}

function submitCalibPoint() {
  const x = parseFloat($("calibX").value), z = parseFloat($("calibZ").value);
  if (isNaN(x) || isNaN(z)) { setStatus("请输入有效的游戏坐标 X/Z", "warning"); return; }
  if (!calib.pendingPixel) return;
  pushCalibHistory();
  calib.points.push({
    pixel: [calib.pendingPixel[0], calib.pendingPixel[1]],
    world: [x, z], enabled: true, error: null, inlier: null,
  });
  calib.mapping = null;
  closeCalibDialog();
  renderCalibList(); renderCalibMarks();
  setStatus("已添加控制点 #" + calib.points.length + "（≥3 个后可计算保存）", "ready");
}

async function saveCalib() {
  const [map, zoom] = (state.activeKey || "").split("@");
  if (!map || !zoom) return;
  const pts = calib.points.map(p => ({ pixel: p.pixel, world: p.world, enabled: !!p.enabled }));
  // 标定矩阵是"总图像素 ↔ 世界坐标"，所以这里要的是整张总图尺寸，不是画布尺寸
  const v0 = state.views.get(state.activeKey);
  const imgSize = hasBounds(v0) ? viewSize(v0) : { w: 0, h: 0 };
  try {
    const resp = await fetch("/api/calib", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        map, zoom, points: pts,
        image_size: { width: imgSize.w, height: imgSize.h },
      }),
    });
    const res = await resp.json();
    if (!res.ok) { addLog("标定失败: " + res.error, "t-err"); return; }
    calib.mapping = res;
    res.control_points.forEach((cp, i) => {
      // 只取拟合结论，别动用户勾选的 enabled：服务端现在把两者分开返回
      if (calib.points[i]) {
        calib.points[i].error = cp.error;
        calib.points[i].inlier = cp.inlier != null ? cp.inlier : cp.enabled;
      }
    });
    renderCalibList(); renderCalibMarks();
    $("calibStatus").innerHTML =
      "已标定 ✅ 平均误差 <b>" + res.average_error + "</b> 最大 <b>" + res.max_error + "</b><br>已保存: <b>" + res.saved + "</b>";
    addLog("标定已保存: " + res.saved + "（平均误差 " + res.average_error + "）", "t-ok");
    updatePlayerMarker(lastPos);
    requestRender();
  } catch (e) { addLog("标定请求失败: " + e.message, "t-err"); }
}

// 切换视图/地图时加载该地图已有标定
async function loadMappingForCurrent() {
  const [map, zoom] = (state.activeKey || "").split("@");
  const wantKey = state.activeKey;  // 请求期间用户可能又切走了
  calib.points = []; calib.mapping = null; calib.key = wantKey;
  calibHistory.undo.length = 0;
  calibHistory.redo.length = 0;
  closeCalibDialog();
  if (!map || !zoom) {
    $("calibStatus").textContent = "未标定（开标定模式后点击地图添加控制点）";
    renderCalibList(); renderCalibMarks(); updatePlayerMarker(null);
    return;
  }
  try {
    const resp = await fetch("/api/calib?map=" + encodeURIComponent(map) + "&zoom=" + encodeURIComponent(zoom));
    const res = await resp.json();
    // 响应可能已经过期：快速来回切地图（或抓取过程中自动切图）时，A 的响应
    // 会晚于 B 到达。这时候 calib.key 已经是 B 而 mapping 是 A，所有
    // `calib.key !== state.activeKey` 的守卫都会通过，于是用错图的仿射矩阵去
    // 换算——涂的格子、路径和玩家标记全部落到错误的世界坐标。
    if (wantKey !== state.activeKey) return;
    if (res.ok && res.matrix) {
      calib.mapping = res;
      calib.points = (res.control_points || []).map(cp => ({
        pixel: cp.pixel, world: cp.world,
        // enabled = 用户勾选（复选框），inlier = 上次拟合是否判为内点（离群标记）。
        // 旧文件只有 enabled，退回用兼容读法。
        enabled: cp.enabled !== false,
        inlier: cp.inlier != null ? cp.inlier : cp.enabled,
        error: cp.error != null ? cp.error : null,
      }));
      $("calibStatus").innerHTML = "已加载标定（平均误差 <b>" + res.average_error + "</b>，最大 " + res.max_error + "）";
      addLog("已加载标定: " + map + "/" + zoom, "t-ok");
    } else {
      $("calibStatus").textContent = "未标定（开标定模式后点击地图添加控制点）";
    }
  } catch (e) {
    if (wantKey !== state.activeKey) return;
    $("calibStatus").textContent = "标定加载失败";
  }
  renderCalibList(); renderCalibMarks();
  updatePlayerMarker(lastPos);
  requestRender();
  updateMarksHint();   // 标定状态决定标记能不能画
  syncTierControls();
}

// 玩家定位：游戏坐标 (x,z) → 地图像素 → 固定屏幕尺寸的十字准星
let lastPos = null;
function updatePlayerMarker(pos) {
  lastPos = pos || lastPos;
  const mk = $("playerMarker");
  if (!lastPos || !calib.mapping || !calib.key || calib.key !== state.activeKey) {
    mk.style.display = "none";
    return;
  }
  const inv = calib.mapping.inverse_matrix;
  const x = Number(lastPos.x), z = Number(lastPos.z);
  if (isNaN(x) || isNaN(z)) { mk.style.display = "none"; return; }
  const px = inv[0][0] * x + inv[0][1] * z + inv[0][2];
  const py = inv[1][0] * x + inv[1][1] * z + inv[1][2];
  const v = state.views.get(state.activeKey);
  const size = hasBounds(v) ? viewSize(v) : { w: 0, h: 0 };
  if (px < -5000 || py < -5000 || px > size.w + 5000 || py > size.h + 5000) {
    mk.style.display = "none";  // 坐标明显在地图外
    return;
  }
  const screenX = px * mapView.scale + mapView.panX;
  const screenY = py * mapView.scale + mapView.panY;
  if (screenX < -24 || screenY < -24 ||
      screenX > canvasWrap.clientWidth + 24 ||
      screenY > canvasWrap.clientHeight + 24) {
    mk.style.display = "none";
    return;
  }
  mk.style.left = screenX + "px";
  mk.style.top = screenY + "px";
  $("playerMarkerLabel").textContent = "(" + x.toFixed(1) + ", " + z.toFixed(1) + ")";
  mk.style.display = "block";
}

// ---------------- 2D 网格编辑（无高度信息，需先标定） ----------------
// 编辑数据挂在每个视图 view.edit 上（切换地图不丢失），全局 edit 指向当前视图
const edit = {
  mode: false, brush: "free", brushSize: 1,
  painting: false, lastCell: null,
  marquee: null,
  v: null, dirty: false,
  pointer: null,   // 光标在 canvasWrap 内的位置，按住画笔键时 HUD 用它定位
  cursorCell: null,
  pendingPaint: null,
  paintFrameQueued: false,
  paintDirty: false,
};
const link = {
  mode: false,
  stage: "source",
  source: null,
  target: null,
  drag: null,
  editingId: null,
};
const editCanvasEl = $("editCanvas");
const ectx = editCanvasEl.getContext("2d");
const selCanvasEl = $("selCanvas");
const sctx = selCanvasEl.getContext("2d");
const linkCanvasEl = $("linkCanvas");
const lctx = linkCanvasEl.getContext("2d");

function ed() { return (edit.v && edit.v.edit) || null; }

function editCellKey(ix, iz) { return ix + "," + iz; }

// 画布像素 → 格子坐标 (ix, iz)：像素→世界(标定矩阵)→格子
function cellAtPixel(px, py) {
  const e = ed();
  if (!e || !calib.mapping || !calib.key || calib.key !== state.activeKey) return null;
  const m = calib.mapping.matrix;
  const wx = m[0][0] * px + m[0][1] * py + m[0][2];
  const wz = m[1][0] * px + m[1][1] * py + m[1][2];
  return [Math.floor((wx - e.origin[0]) / e.cellSize),
          Math.floor((wz - e.origin[1]) / e.cellSize)];
}

// ---- 世界↔画布像素换算 ----
// 这组函数在 drawAllCells 里是**逐格**调用的，所以一律写进调用方给的 out、
// 不建闭包也不返回新对象：原来每格要分配一个闭包 + 两个数组 + 一个对象字面量。

// 格子角点 (ix, iz) → 画布像素，写进 out[0]/out[1]。不做 min/abs 归一化：
// 标定矩阵可能翻转某个轴，方向要由矩阵本身决定。
function cellCornerPx(ix, iz, out) {
  const e = ed(), inv = calib.mapping.inverse_matrix, s = e.cellSize;
  const wx = e.origin[0] + ix * s, wz = e.origin[1] + iz * s;
  out[0] = inv[0][0] * wx + inv[0][1] * wz + inv[0][2];
  out[1] = inv[1][0] * wx + inv[1][1] * wz + inv[1][2];
  return out;
}

// 格子空间的矩形 [x0..x1]×[z0..z1] → 画布像素包围盒。
// 必须四个角都变换再取包围盒：标定可能翻转某个轴，只取两个 min 角点会缺一条边。
function cellRangeRectPx(x0, z0, x1, z1) {
  const a = cellCornerPx(x0, z0, [0, 0]), b = cellCornerPx(x1, z0, [0, 0]);
  const c = cellCornerPx(x0, z1, [0, 0]), d = cellCornerPx(x1, z1, [0, 0]);
  const bx = Math.min(a[0], b[0], c[0], d[0]), by = Math.min(a[1], b[1], c[1], d[1]);
  return { x: bx, y: by,
           w: Math.max(a[0], b[0], c[0], d[0]) - bx,
           h: Math.max(a[1], b[1], c[1], d[1]) - by };
}

// 保存后文件的真实范围：有 shape 时是 [0,W)×[0,H)（读文件时带回来的原范围），
// 再把涂到范围外的格子并进来。没有 shape（新建网格）才退回已涂格子的边界盒。
// 这张黄框也是连通性判定的边界——规划器把数组外一律当阻挡。
function savedBounds(e) {
  return e.savedBounds();
}

function gridCellToPixelTransform(e) {
  const inv = calib.mapping.inverse_matrix;
  const scale = e.cellSize;
  const ox = e.origin[0];
  const oz = e.origin[1];
  return {
    a00: inv[0][0] * scale,
    a01: inv[0][1] * scale,
    a02: inv[0][0] * ox + inv[0][1] * oz + inv[0][2],
    a10: inv[1][0] * scale,
    a11: inv[1][1] * scale,
    a12: inv[1][0] * ox + inv[1][1] * oz + inv[1][2],
  };
}

function invalidateGridLayer() {
  gridLayerState.valid = false;
}

function updateGridBoundsOverlay(e) {
  const bb = e ? savedBounds(e) : null;
  if (!bb || !calib.mapping || (!edit.mode && !link.mode) || !state.layers.grid) {
    gridBoundsEl.style.display = "none";
    return;
  }
  const rect = cellRangeRectPx(bb.x0, bb.z0, bb.x1 + 1, bb.z1 + 1);
  gridBoundsEl.style.left = rect.x + "px";
  gridBoundsEl.style.top = rect.y + "px";
  gridBoundsEl.style.width = rect.w + "px";
  gridBoundsEl.style.height = rect.h + "px";
  gridBoundsEl.style.borderWidth = Math.max(0.5, 1 / mapView.scale) + "px";
  gridBoundsEl.style.display = "block";
}

function drawAllCells() {
  const vis = visibleRect();
  const e = ed();
  if (!e || !calib.mapping) return;
  const transform = gridCellToPixelTransform(e);
  const layerKey = [
    state.activeKey,
    vis.x, vis.y, vis.w, vis.h,
    transform.a00, transform.a01, transform.a02,
    transform.a10, transform.a11, transform.a12,
    editCanvasEl.width, editCanvasEl.height,
  ].join("|");
  const fullRedraw = !gridLayerState.valid ||
    gridLayerState.key !== layerKey ||
    gridLayerState.width !== editCanvasEl.width ||
    gridLayerState.height !== editCanvasEl.height;
  const changed = gridRasterCache.sync(e);
  updateGridBoundsOverlay(e);

  if (fullRedraw) {
    ectx.clearRect(vis.x, vis.y, vis.w, vis.h);
    ectx.save();
    ectx.globalAlpha = state.gridOpacity;
    gridRasterCache.drawAll(ectx, e, transform, vis);
    ectx.restore();
    gridLayerState.valid = true;
    gridLayerState.key = layerKey;
    gridLayerState.width = editCanvasEl.width;
    gridLayerState.height = editCanvasEl.height;
    return;
  }

  if (changed.length) {
    ectx.save();
    ectx.globalAlpha = state.gridOpacity;
    gridRasterCache.drawChanged(ectx, e, transform, vis, changed);
    ectx.restore();
  }
}

// ---------------- 框选（Shift+左键拖动） ----------------
// 选中层画在**独立画布**（selCanvas）上：框选时每次鼠标移动只需重画这一个矩形，
// 不必把成千上万个格子整屏重绘一遍。选择只可能由矩形框选产生，所以永远是个矩形。
function selRectOf() {
  const m = edit.marquee;
  if (m && m.cur) {
    return { x0: Math.min(m.ax, m.cur[0]), x1: Math.max(m.ax, m.cur[0]),
             z0: Math.min(m.az, m.cur[1]), z1: Math.max(m.az, m.cur[1]) };
  }
  const v = state.views.get(state.activeKey);
  return (v && v.edit && v.edit.selRect) || null;
}

function drawSelLayer() {
  // 切换地图的瞬间标定可能还没加载完（calib.mapping 为 null），此时坐标换算无从谈起
  if (selCanvasEl.width <= 1 || !calib.mapping) return;
  const vis = visibleRect();
  sctx.clearRect(vis.x, vis.y, vis.w, vis.h);
  drawFloorRegionOverlay();
  if (!edit.mode) return;
  const r = selRectOf();
  if (!r) return;
  const p = cellRangeRectPx(r.x0, r.z0, r.x1 + 1, r.z1 + 1);
  sctx.fillStyle = "rgba(140,90,255,0.18)";
  sctx.fillRect(p.x, p.y, p.w, p.h);
  sctx.strokeStyle = "rgba(190,150,255,0.72)";
  sctx.lineWidth = 1;
  sctx.strokeRect(p.x + 0.5, p.y + 0.5, Math.max(0, p.w), Math.max(0, p.h));
}

function floorRegionRect(region) {
  return {
    x0: Number(region.x0), x1: Number(region.x1),
    z0: Number(region.z0), z1: Number(region.z1),
    y_min: Number(region.y_min), y_max: Number(region.y_max),
  };
}

function drawWorldPolygon(g, rect, fill, stroke, dashed = false) {
  const points = worldRectPoints(rect, calib.mapping.inverse_matrix);
  g.save();
  g.beginPath();
  g.moveTo(points[0][0], points[0][1]);
  for (let i = 1; i < points.length; i++) {
    g.lineTo(points[i][0], points[i][1]);
  }
  g.closePath();
  if (fill) {
    g.fillStyle = fill;
    g.fill();
  }
  if (stroke) {
    g.setLineDash(dashed
      ? [6 / mapView.scale, 4 / mapView.scale]
      : []);
    g.strokeStyle = stroke;
    g.lineWidth = Math.max(0.5, 1 / mapView.scale);
    g.stroke();
  }
  g.restore();
}

function drawOverlapCellSet(g, cells, fill) {
  if (!cells || !cells.size) return;
  if (!ed() || !calib.mapping) return;
  const vis = visibleRect();
  g.save();
  g.fillStyle = fill;
  g.beginPath();
  for (const key of cells) {
    const comma = key.indexOf(",");
    const ix = Number(key.slice(0, comma));
    const iz = Number(key.slice(comma + 1));
    const rect = cellRangeRectPx(ix, iz, ix + 1, iz + 1);
    if (rect.x + rect.w < vis.x || rect.x > vis.x + vis.w) continue;
    if (rect.y + rect.h < vis.y || rect.y > vis.y + vis.h) continue;
    g.rect(rect.x, rect.y, rect.w, rect.h);
  }
  g.fill();
  g.restore();
}

function drawFloorComponentHighlight() {
  const id = state.floorHighlight.componentId;
  if (!id) return;
  const component = (state.floorOverlap.components || [])
    .find(item => item.id === id);
  if (!component) return;
  const palette = ["#66ddff", "#ffb84d", "#ff72d2", "#8dff9b", "#d0a6ff"];
  component.layers.forEach((tierId, index) => {
    const layer = (state.floorMap.layers || [])
      .find(item => item.tier_id === tierId);
    if (!layer) return;
    // 只画这条连通区自己认领的区间，别把同层其它/历史标记一起拖出来。
    const region = floorRegionForComponent(layer, component);
    if (!region) return;
    const selectedLayer = tierId === state.floorHighlight.layerId;
    drawWorldPolygon(
      sctx, floorRegionRect(region),
      selectedLayer ? "rgba(255,255,255,0.12)" : "rgba(255,255,255,0.035)",
      palette[index % palette.length],
      !selectedLayer,
    );
  });
  if (!ed() || !calib.mapping) return;
  sctx.save();
  sctx.fillStyle = "rgba(255,255,255,0.30)";
  sctx.strokeStyle = "rgba(255,118,232,0.98)";
  sctx.lineWidth = Math.max(1, 1.5 / mapView.scale);
  for (const cell of component.cells || []) {
    const ix = Number(cell[0]);
    const iz = Number(cell[1]);
    const rect = cellRangeRectPx(ix, iz, ix + 1, iz + 1);
    sctx.fillRect(rect.x, rect.y, rect.w, rect.h);
    sctx.strokeRect(rect.x, rect.y, rect.w, rect.h);
  }
  sctx.restore();
}

// 已保存的标记统一填蓝：不管它属于哪一层、还跟不跟得上当前连通区，
// 都能看见、也能回去改，避免旧标记"隐身"。
function drawMarkedRegion(g, region) {
  if (!region || !calib.mapping) return;
  const cells = Array.isArray(region.cells) ? region.cells : [];
  const valid = cells.filter(cell =>
    Array.isArray(cell) && cell.length >= 4 &&
    Number.isFinite(Number(cell[0])) && Number.isFinite(Number(cell[1])) &&
    Number.isFinite(Number(cell[2])) && Number.isFinite(Number(cell[3])));
  if (valid.length) {
    g.save();
    g.fillStyle = "rgba(70,190,255,0.46)";
    g.beginPath();
    for (const cell of valid) {
      const points = worldRectPoints({
        x0: Number(cell[0]), x1: Number(cell[1]),
        z0: Number(cell[2]), z1: Number(cell[3]),
      }, calib.mapping.inverse_matrix);
      g.moveTo(points[0][0], points[0][1]);
      for (let i = 1; i < points.length; i++) {
        g.lineTo(points[i][0], points[i][1]);
      }
      g.closePath();
    }
    g.fill();
    g.restore();
  }
  drawWorldPolygon(g, floorRegionRect(region), null, "rgba(120,215,255,0.9)");
}

function drawFloorRegionOverlay() {
  const data = state.floorMap;
  if (!data || !data.layers || !data.layers.length) return;
  const active = currentGridKey();
  const current = data.layers.find(layer => layer.tier_id === active);
  // 默认只画当前网格自己的标记，免得别的分层的历史标记跑到大地图上。
  if (current) {
    for (const region of current.floor_regions || []) {
      drawMarkedRegion(sctx, region);
      drawWorldPolygon(
        sctx, floorRegionRect(region), null,
        "rgba(118,255,159,0.9)",
      );
    }
  }
  // 选中的标记区间：即使它属于别的分层，也照样显示范围。
  const marking = selectedFloorMarking();
  if (marking && (!current || marking.layer.tier_id !== active)) {
    drawMarkedRegion(sctx, marking.region);
  }
  // 选中的重合连通区：它涉及的每一层，只画认领到的那一条区间。
  const component = selectedFloorComponent();
  if (component) {
    for (const tierId of component.layers) {
      if (current && tierId === active) continue;
      const layer = data.layers.find(item => item.tier_id === tierId);
      const region = layer ? floorRegionForComponent(layer, component) : null;
      if (region) drawMarkedRegion(sctx, region);
    }
  }
  drawOverlapCellSet(
    sctx, state.floorOverlap.xzOnly, "rgba(255,213,64,0.48)");
  drawOverlapCellSet(
    sctx, state.floorOverlap.threeD, "rgba(255,86,86,0.52)");
  drawFloorComponentHighlight();
  if (marking) {
    drawWorldPolygon(
      sctx, floorRegionRect(marking.region), null,
      "rgba(255,214,90,0.95)",
    );
  }
}

function floorWorldCellKey(x, z) {
  return Number(x).toFixed(3) + "," + Number(z).toFixed(3);
}

async function loadFloorPeerGrids(force = false) {
  const key = state.activeKey;
  if (!key || !(edit.mode || link.mode)) return;
  if (!force && state.floorPeerGrids.size) return;
  const currentKey = currentGridKey();
  const [map, zoom] = key.split("@");
  const candidates = (state.floorMap.layers || []).filter(layer =>
    layer.has_grid && layer.tier_id !== currentKey
  );
  const loaded = new Map();
  await Promise.all(candidates.map(async layer => {
    const requestTier = layer.tier_id === "__main__" ? "" : layer.tier_id;
    try {
      const result = await fetch(
        "/api/grid2d?map=" + encodeURIComponent(map) +
        "&zoom=" + encodeURIComponent(zoom) +
        "&tier=" + encodeURIComponent(requestTier),
      ).then(response => response.json());
      if (!result.data) return;
      const origin = result.data.origin || [0, 0, 0];
      const size = Number(result.data.cell_size) || 1;
      const cells = new Set();
      for (const [ix, iz] of result.data.cells || []) {
        cells.add(floorWorldCellKey(
          Number(origin[0]) + (Number(ix) + 0.5) * size,
          Number(origin[2]) + (Number(iz) + 0.5) * size,
        ));
      }
      for (const [ix, iz] of result.data.blocked || []) {
        cells.add(floorWorldCellKey(
          Number(origin[0]) + (Number(ix) + 0.5) * size,
          Number(origin[2]) + (Number(iz) + 0.5) * size,
        ));
      }
      loaded.set(layer.tier_id, cells);
    } catch (e) {
      addLog("加载重合对比网格失败 " + layer.tier_id + ": " + e.message, "t-warn");
    }
  }));
  if (state.activeKey !== key || currentGridKey() !== currentKey) return;
  state.floorPeerGrids = loaded;
  recomputeFloorOverlapLive();
}

function recomputeFloorOverlapLive() {
  const document = ed();
  if (!document || !calib.mapping || !state.floorPeerGrids.size) {
    requestRender(RENDER_LAYERS.SELECTION);
    return;
  }
  const live = new Set();
  document.forEachCell((ix, iz, value) => {
    if (!value) return;
    const x = document.origin[0] + (ix + 0.5) * document.cellSize;
    const z = document.origin[1] + (iz + 0.5) * document.cellSize;
    const worldKey = floorWorldCellKey(x, z);
    for (const peer of state.floorPeerGrids.values()) {
      if (peer.has(worldKey)) {
        live.add(ix + "," + iz);
        break;
      }
    }
  });
  const yellow = new Set();
  for (const cell of live) {
    if (state.floorOverlap.threeD.has(cell) ||
        state.floorOverlap.resolved.has(cell)) {
      continue;
    }
    yellow.add(cell);
  }
  state.floorOverlap.xzOnly = yellow;
  requestRender(RENDER_LAYERS.SELECTION);
}

function scheduleFloorOverlapLiveRefresh() {
  if (!(edit.mode || link.mode)) return;
  if (state.floorLiveRefreshTimer) {
    clearTimeout(state.floorLiveRefreshTimer);
  }
  state.floorLiveRefreshTimer = setTimeout(() => {
    state.floorLiveRefreshTimer = 0;
    void loadFloorPeerGrids(false).then(() => recomputeFloorOverlapLive());
  }, 80);
}

function updateSelBar() {
  const r = selRectOf();
  // 选择只可能是矩形，面积 O(1) 算出，不需要展开为逐格集合。
  $("selCount").textContent = r ? (r.x1 - r.x0 + 1) * (r.z1 - r.z0 + 1) : 0;
}

function clearSelection() {
  const v = state.views.get(state.activeKey);
  if (v && v.edit) v.edit.selRect = null;
  requestRender(RENDER_LAYERS.SELECTION);
  updateSelBar();
}

function floorMarkingKey(layer, region) {
  const tierId = (layer && layer.tier_id) || "__main__";
  const index = ((layer && layer.floor_regions) || []).indexOf(region);
  return "m:" + tierId + ":" + index;
}

// 已经保存、但没有被当前任何重合连通区认领的标记（多半是基准挪动后留下的旧标记）。
function floorMarkingsNotInComponents() {
  const components = state.floorOverlap.components || [];
  const claimed = new Set();
  for (const component of components) {
    for (const tierId of component.layers) {
      const layer = (state.floorMap.layers || [])
        .find(item => item.tier_id === tierId);
      const region = layer ? floorRegionForComponent(layer, component) : null;
      if (region) claimed.add(region);
    }
  }
  const out = [];
  for (const layer of state.floorMap.layers || []) {
    for (const region of layer.floor_regions || []) {
      if (!claimed.has(region)) out.push({ layer, region });
    }
  }
  return out;
}

function populateFloorComponentSelect() {
  const select = $("floorComponent");
  if (!select) return;
  const previous = select.value;
  const components = state.floorOverlap.components || [];
  const options = components.map((component, index) => {
    const label = "连通区 " + (index + 1) +
      " · " + component.severity +
      " · " + (component.resolved ? "已标记" : "未标记") +
      " · " + component.cells.length + " 格 · " +
      component.layer_names.join(" / ");
    return '<option value="c:' + component.id + '">' + label + "</option>";
  });
  for (const { layer, region } of floorMarkingsNotInComponents()) {
    const label = "已标记 · " + (layer.name || layer.tier_id) +
      " · Y " + region.y_min + " ~ " + region.y_max +
      " · " + ((region.cells || []).length) + " 格";
    options.push('<option value="' + floorMarkingKey(layer, region) + '">' +
      label + "</option>");
  }
  select.innerHTML = options.length
    ? options.join("")
    : '<option value="">暂无重合连通区</option>';
  select.disabled = !options.length;
  if (options.length) {
    const values = [...select.options].map(option => option.value);
    select.value = values.includes(previous) ? previous : values[0];
  }
}

function selectedFloorTarget() {
  const value = $("floorComponent").value || "";
  if (value.startsWith("c:")) {
    const id = value.slice(2);
    const component = (state.floorOverlap.components || [])
      .find(item => item.id === id);
    return component ? { kind: "component", component } : null;
  }
  if (value.startsWith("m:")) {
    const rest = value.slice(2);
    const sep = rest.lastIndexOf(":");
    const tierId = rest.slice(0, sep);
    const index = Number(rest.slice(sep + 1));
    const layer = (state.floorMap.layers || [])
      .find(item => (item.tier_id || "__main__") === tierId);
    const region = layer && (layer.floor_regions || [])[index];
    return layer && region ? { kind: "marking", layer, region } : null;
  }
  return null;
}

function selectedFloorComponent() {
  const target = selectedFloorTarget();
  return target && target.kind === "component" ? target.component : null;
}

function selectedFloorMarking() {
  const target = selectedFloorTarget();
  return target && target.kind === "marking" ? target : null;
}

function componentCellCenters(component) {
  const document = ed();
  if (!document) return new Set();
  return new Set((component.cells || []).map(cell => {
    const x = document.origin[0] + (Number(cell[0]) + 0.5) * document.cellSize;
    const z = document.origin[1] + (Number(cell[1]) + 0.5) * document.cellSize;
    return x.toFixed(3) + "," + z.toFixed(3);
  }));
}

function floorRegionForComponent(layer, component) {
  if (!layer) return null;
  const regions = layer.floor_regions || [];
  const active = regions.find(region =>
    region.component_id === component.id);
  if (active) return active;
  const componentCenters = componentCellCenters(component);
  if (!componentCenters.size) {
    return regions.find(region =>
      Number(region.x0) <= Number(component.world_bounds && component.world_bounds.x1) &&
      Number(region.x1) >= Number(component.world_bounds && component.world_bounds.x0) &&
      Number(region.z0) <= Number(component.world_bounds && component.world_bounds.z1) &&
      Number(region.z1) >= Number(component.world_bounds && component.world_bounds.z0)
    ) || null;
  }
  let best = null;
  let bestRatio = 0;
  for (const region of regions) {
    const cells = region.cells || [];
    if (!cells.length) continue;
    const regionCenters = new Set(cells.map(cell =>
      ((Number(cell[0]) + Number(cell[1])) / 2).toFixed(3) + "," +
      ((Number(cell[2]) + Number(cell[3])) / 2).toFixed(3)
    ));
    let overlap = 0;
    for (const center of componentCenters) {
      if (regionCenters.has(center)) overlap++;
    }
    const ratio = overlap / Math.min(
      componentCenters.size, regionCenters.size);
    if (ratio > bestRatio) {
      bestRatio = ratio;
      best = region;
    }
  }
  if (bestRatio >= 0.8) return best;
  // 网格基准挪动过以后，旧标记的格子可能只和当前连通区部分相交。
  // 只要包围盒相交，就仍然当作该连通区的旧标记，方便直接改掉。
  const bounds = component.world_bounds;
  if (!bounds) return null;
  let hit = null;
  let hitArea = 0;
  for (const region of regions) {
    const ix0 = Math.max(Number(region.x0), Number(bounds.x0));
    const ix1 = Math.min(Number(region.x1), Number(bounds.x1));
    const iz0 = Math.max(Number(region.z0), Number(bounds.z0));
    const iz1 = Math.min(Number(region.z1), Number(bounds.z1));
    if (!(ix1 > ix0 && iz1 > iz0)) continue;
    const area = (ix1 - ix0) * (iz1 - iz0);
    if (area > hitArea) {
      hitArea = area;
      hit = region;
    }
  }
  return hit;
}

function populateFloorLayerSelect() {
  const select = $("floorLayer");
  if (!select) return;
  const target = selectedFloorTarget();
  const previous = select.value;
  let entries = [];
  if (target && target.kind === "component") {
    const component = target.component;
    entries = component.layers.map((tierId, index) => {
        const layer = (state.floorMap.layers || [])
          .find(item => item.tier_id === tierId);
        const region = layer ? floorRegionForComponent(layer, component) : null;
        const mark = region
          ? "已标记 Y " + region.y_min + " ~ " + region.y_max
          : "未标记";
        return {
          tierId,
          label:
          (component.layer_names[index] || tierId) + " · " + tierId +
          " · " + mark,
        };
      });
  } else if (target && target.kind === "marking") {
    const layer = target.layer;
    entries = [{
      tierId: layer.tier_id,
      label: (layer.name || layer.tier_id) +
        " · Y " + target.region.y_min + " ~ " + target.region.y_max +
        " · 已标记",
    }];
  }
  select.innerHTML = entries.length
    ? entries.map(entry =>
        '<option value="' + entry.tierId + '">' + entry.label + "</option>"
      ).join("")
    : '<option value="">暂无重合网格</option>';
  select.disabled = !entries.length;
  if (entries.some(entry => entry.tierId === previous)) {
    select.value = previous;
  }
}

function selectedFloorLayerId() {
  return $("floorLayer").value || "";
}

function currentGridKey() {
  return currentTierId() || "__main__";
}

// 标记对话框停在画布中央，定位时把目标放到左侧偏下，免得被对话框挡住。
function focusWorldBounds(bounds, anchorX = 0.30, anchorY = 0.70) {
  if (!bounds || !calib.mapping) return;
  const width = Math.max(1, Number(bounds.x1) - Number(bounds.x0));
  const height = Math.max(1, Number(bounds.z1) - Number(bounds.z0));
  const desired = Math.min(
    canvasWrap.clientWidth * 0.55 / width,
    canvasWrap.clientHeight * 0.55 / height,
    4,
  );
  mapView.scale = Math.max(mapView.scale, Math.max(0.05, desired));
  const cx = (Number(bounds.x0) + Number(bounds.x1)) / 2;
  const cz = (Number(bounds.z0) + Number(bounds.z1)) / 2;
  const inv = calib.mapping.inverse_matrix;
  const px = inv[0][0] * cx + inv[0][1] * cz + inv[0][2];
  const py = inv[1][0] * cx + inv[1][1] * cz + inv[1][2];
  mapView.panX = canvasWrap.clientWidth * anchorX - px * mapView.scale;
  mapView.panY = canvasWrap.clientHeight * anchorY - py * mapView.scale;
  mapView.focus = null;
  clampPan();
  applyView(true);
}

function focusFloorComponent(component) {
  focusWorldBounds(component && component.world_bounds);
}

function floorLayerKey(layer) {
  const tierId = layer && layer.tier_id;
  return !tierId || tierId === "__main__" ? "" : tierId;
}

function renderFloorMarkedList() {
  const box = $("floorMarkedList");
  if (!box) return;
  const rows = [];
  for (const layer of state.floorMap.layers || []) {
    for (const region of layer.floor_regions || []) {
      rows.push({ layer, region });
    }
  }
  state.floorMarkedRows = rows;
  const count = $("floorMarkedCount");
  if (count) count.textContent = rows.length ? "（" + rows.length + "）" : "";
  if (!rows.length) {
    box.innerHTML = '<div class="floor-marked-empty">暂无已标记区间</div>';
    return;
  }
  box.innerHTML = rows.map(({ layer, region }, index) => {
    const cells = Array.isArray(region.cells) ? region.cells.length : 0;
    const label = (layer.name || layer.tier_id) +
      " · Y " + region.y_min + " ~ " + region.y_max +
      " · " + cells + "格";
    return '<div class="floor-marked-row">' +
      "<span>" + label + "</span>" +
      '<button type="button" data-floor-focus="' + index + '">定位</button>' +
      '<button type="button" data-floor-remove="' + index + '">删除</button>' +
      "</div>";
  }).join("");
}

function focusFloorRegion(region) {
  if (!region) return;
  focusWorldBounds({
    x0: Number(region.x0), x1: Number(region.x1),
    z0: Number(region.z0), z1: Number(region.z1),
  });
}

async function postFloorRegions(layer, regions) {
  const [map, zoom] = (state.activeKey || "@").split("@");
  const res = await fetch("/api/grid2d/floor-regions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      map, zoom, tier: floorLayerKey(layer), floor_regions: regions,
    }),
  }).then(response => response.json());
  if (!res.ok) throw new Error(res.error || "保存失败");
  return res;
}

async function removeFloorRegion(layer, region) {
  if (!layer || !region) return;
  try {
    await postFloorRegions(
      layer, (layer.floor_regions || []).filter(item => item !== region));
    await loadFloorRegionMap(true);
    await loadFloorOverlaps(true);
    renderFloorMarkedList();
    $("floorRegionStatus").textContent = "已删除 1 条已标记区间";
  } catch (e) {
    $("floorRegionStatus").textContent = e.message;
  }
}

function fillFloorHeightFromSelection() {
  const target = selectedFloorTarget();
  if (!target) {
    state.floorHighlight = { componentId: "", layerId: "" };
    requestRender(RENDER_LAYERS.SELECTION);
    return;
  }
  if (target.kind === "marking") {
    const { layer, region } = target;
    const key = floorMarkingKey(layer, region);
    const changed = state.floorHighlight.componentId !== key;
    state.floorHighlight.componentId = key;
    state.floorHighlight.layerId = layer.tier_id;
    populateFloorLayerSelect();
    $("floorYMin").value = region.y_min;
    $("floorYMax").value = region.y_max;
    $("floorRegionStatus").textContent =
      "已标记区间（" + (layer.name || layer.tier_id) + "）Y " +
      region.y_min + " ~ " + region.y_max +
      "；改完点“标记所选网格”覆盖，或点“清除所选网格区间”删除";
    if (changed) focusFloorRegion(region);
    requestRender(RENDER_LAYERS.SELECTION);
    return;
  }
  const component = target.component;
  const componentChanged =
    state.floorHighlight.componentId !== component.id;
  state.floorHighlight.componentId = component.id;
  populateFloorLayerSelect();
  const tierId = selectedFloorLayerId();
  state.floorHighlight.layerId = tierId;
  if (componentChanged) focusFloorComponent(component);
  const layer = (state.floorMap.layers || [])
    .find(item => item.tier_id === tierId);
  const region = floorRegionForComponent(layer, component);
  if (region) {
    $("floorYMin").value = region.y_min;
    $("floorYMax").value = region.y_max;
    $("floorRegionStatus").textContent =
      (component.resolved ? "该重合区各层都已标记；" : "") +
      "此层已标记 Y " + region.y_min + " ~ " + region.y_max +
      "（改完点“标记所选网格”即覆盖旧区间）；涉及 " +
      component.layer_names.join(" / ");
  } else {
    const y = Number(lastPos && lastPos.y);
    $("floorYMin").value = Number.isFinite(y) ? y - 20 : "";
    $("floorYMax").value = Number.isFinite(y) ? y + 20 : "";
    $("floorRegionStatus").textContent =
      (component.resolved ? "该重合区各层都已标记；" : "") +
      "此层尚未标记该重合区；涉及 " +
      component.layer_names.join(" / ");
  }
  requestRender(RENDER_LAYERS.SELECTION);
}

function componentWorldCenter(component) {
  const bounds = component && component.world_bounds;
  if (!bounds) return null;
  return [
    (Number(bounds.x0) + Number(bounds.x1)) / 2,
    (Number(bounds.z0) + Number(bounds.z1)) / 2,
  ];
}

// 切换正在编辑的分层网格，切完再把原来选中的目标落回去。
async function switchFloorTier(tierId, restore) {
  const next = tierId || "";
  if ((currentTierId() || "") === next) {
    if (typeof restore === "function") restore();
    return;
  }
  await setActiveTier(next);
  await loadFloorRegionMap(true);
  if (typeof restore === "function") restore();
  else fillFloorHeightFromSelection();
}

function selectFloorComponentBySignature(component) {
  const signature = [...component.layers].sort().join("|");
  const options = (state.floorOverlap.components || [])
    .filter(item => [...item.layers].sort().join("|") === signature);
  if (!options.length) return null;
  const center = componentWorldCenter(component);
  if (!center) return options[0];
  const distance = item => {
    const other = componentWorldCenter(item);
    return other
      ? Math.hypot(other[0] - center[0], other[1] - center[1])
      : Infinity;
  };
  return options.slice().sort((a, b) => distance(a) - distance(b))[0];
}

async function onFloorLayerChange() {
  const target = selectedFloorTarget();
  if (!target || target.kind !== "component") {
    fillFloorHeightFromSelection();
    return;
  }
  const tierId = selectedFloorLayerId();
  const next = tierId === "__main__" ? "" : tierId;
  if (next !== (currentTierId() || "")) {
    const previous = target.component;
    $("floorRegionStatus").textContent = "正在切换到该分层的网格…";
    await switchFloorTier(next, () => {
      const match = selectFloorComponentBySignature(previous);
      if (match) $("floorComponent").value = "c:" + match.id;
      fillFloorHeightFromSelection();
    });
    return;
  }
  fillFloorHeightFromSelection();
}

function restoreFloorTarget(value) {
  const select = $("floorComponent");
  if (!select) return false;
  if ([...select.options].some(option => option.value === value)) {
    select.value = value;
    fillFloorHeightFromSelection();
    return true;
  }
  return false;
}

async function onFloorTargetChange() {
  const target = selectedFloorTarget();
  if (target && target.kind === "marking") {
    const tierId = target.layer.tier_id || "__main__";
    const next = tierId === "__main__" ? "" : tierId;
    if (next !== (currentTierId() || "")) {
      const regionIndex = (target.layer.floor_regions || [])
        .indexOf(target.region);
      $("floorRegionStatus").textContent =
        "正在切换到 " + (target.layer.name || tierId) + " 的网格…";
      await switchFloorTier(next, () => {
        if (restoreFloorTarget("m:" + tierId + ":" + regionIndex)) return;
        // 换到该分层自己视角后，这条标记多半已被某个连通区认领，直接选中它。
        const layer = (state.floorMap.layers || [])
          .find(item => (item.tier_id || "__main__") === tierId);
        const region = layer && (layer.floor_regions || [])[regionIndex];
        const owner = region && (state.floorOverlap.components || [])
          .find(component =>
            floorRegionForComponent(layer, component) === region);
        if (owner) $("floorComponent").value = "c:" + owner.id;
        fillFloorHeightFromSelection();
      });
      return;
    }
  }
  fillFloorHeightFromSelection();
}

function openFloorRegionDialog() {
  if (!edit.mode && !link.mode) {
    setStatus("请先进入网格或连接模式", "warning");
    return;
  }
  const dialog = $("floorRegionDlg");
  populateFloorComponentSelect();
  fillFloorHeightFromSelection();
  renderFloorMarkedList();
  dialog.style.display = "block";
}

function closeFloorRegionDialog() {
  $("floorRegionDlg").style.display = "none";
  if (state.floorMap.key && state.floorMap.key !== mapKey) {
    state.floorHighlight = { componentId: "", layerId: "" };
  }
  requestRender(RENDER_LAYERS.SELECTION);
}

function floorHeightFromInputs() {
  const yMin = Number($("floorYMin").value);
  const yMax = Number($("floorYMax").value);
  if (!Number.isFinite(yMin) || !Number.isFinite(yMax)) {
    throw new Error("Y MIN/Y MAX 必须是有效数字");
  }
  if (yMin > yMax) throw new Error("Y MIN 不能大于 Y MAX");
  return { y_min: yMin, y_max: yMax };
}

async function saveFloorRegion() {
  const target = selectedFloorTarget();
  if (!target) {
    $("floorRegionStatus").textContent = "请先选择重合连通区或已标记区间";
    return;
  }
  let height;
  try {
    height = floorHeightFromInputs();
  } catch (e) {
    $("floorRegionStatus").textContent = e.message;
    return;
  }
  if (target.kind === "marking") {
    const layer = target.layer;
    const regions = (layer.floor_regions || []).map(item =>
      item === target.region
        ? { ...item, y_min: height.y_min, y_max: height.y_max }
        : item);
    try {
      await postFloorRegions(layer, regions);
      await loadFloorRegionMap(true);
      await loadFloorOverlaps(true);
      renderFloorMarkedList();
      $("floorRegionStatus").textContent =
        "已更新该标记为 Y " + height.y_min + " ~ " + height.y_max;
    } catch (e) {
      $("floorRegionStatus").textContent = e.message;
    }
    return;
  }
  const component = target.component;
  const tierId = selectedFloorLayerId();
  if (!tierId) {
    $("floorRegionStatus").textContent = "请先选择重合网格";
    return;
  }
  const [map, zoom] = (state.activeKey || "@").split("@");
  const targetLayer = (state.floorMap.layers || [])
    .find(item => item.tier_id === tierId);
  const previous = floorRegionForComponent(targetLayer, component);
  const replaceComponentIds = previous && previous.component_id
    ? { [tierId]: [previous.component_id] }
    : {};
  try {
    const res = await fetch("/api/grid2d/overlap-components/height", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        map,
        zoom,
        tier: currentGridKey(),
        level_id: state.floorMap.levelId,
        component_id: component.id,
        assignments: [{
          tier_id: tierId,
          y_min: height.y_min,
          y_max: height.y_max,
        }],
        replace_component_ids: replaceComponentIds,
      }),
    }).then(response => response.json());
    if (!res.ok) throw new Error(res.error || "保存失败");
    await loadFloorRegionMap(true);
    await loadFloorOverlaps(true);
    renderFloorMarkedList();
    const remaining = unmarkedYellowComponents().length;
    $("floorRegionStatus").textContent =
      "已标记 " + (component.layer_names[component.layers.indexOf(tierId)] || tierId) +
      " 的 Y " + height.y_min + " ~ " + height.y_max +
      (remaining ? "；仍有 " + remaining + " 个黄色连通区未完成" : "；全部重合区已完成");
    if (remaining) {
      addLog("仍有 " + remaining + " 个黄色重合连通区未定义高度区间", "t-warn");
    }
    void loadNavReadiness();
  } catch (e) {
    $("floorRegionStatus").textContent = e.message;
  }
}

async function clearFloorRegions() {
  const target = selectedFloorTarget();
  if (!target) {
    $("floorRegionStatus").textContent = "请先选择要清除的标记";
    return;
  }
  let layer;
  let regions;
  if (target.kind === "marking") {
    layer = target.layer;
    regions = (layer.floor_regions || [])
      .filter(item => item !== target.region);
  } else {
    const tierId = selectedFloorLayerId();
    if (!tierId) {
      $("floorRegionStatus").textContent = "请先选择要清除的重合网格";
      return;
    }
    layer = (state.floorMap.layers || [])
      .find(item => item.tier_id === tierId);
    regions = (layer && layer.floor_regions || [])
      .filter(item =>
        !floorRegionForComponent({ floor_regions: [item] }, target.component));
  }
  try {
    await postFloorRegions(layer, regions);
    await loadFloorRegionMap(true);
    await loadFloorOverlaps(true);
    renderFloorMarkedList();
    $("floorRegionStatus").textContent = target.kind === "marking"
      ? "已删除该已标记区间"
      : "已清除所选网格在此重合区的区间";
    void loadNavReadiness();
  } catch (e) {
    $("floorRegionStatus").textContent = e.message;
  }
}

function worldRectPoints(rect, inv) {
  const points = [
    [rect.x0, rect.z0], [rect.x1, rect.z0],
    [rect.x1, rect.z1], [rect.x0, rect.z1],
  ];
  return points.map(([x, z]) => [
    inv[0][0] * x + inv[0][1] * z + inv[0][2],
    inv[1][0] * x + inv[1][1] * z + inv[1][2],
  ]);
}

function drawWorldRect(g, rect, inv, fill, stroke) {
  const points = worldRectPoints(rect, inv);
  g.beginPath();
  g.moveTo(points[0][0], points[0][1]);
  for (let i = 1; i < points.length; i++) g.lineTo(points[i][0], points[i][1]);
  g.closePath();
  g.fillStyle = fill;
  g.fill();
  g.strokeStyle = stroke;
  g.lineWidth = 1 / mapView.scale;
  g.stroke();
  return points;
}

function endpointIsCurrent(endpoint) {
  const [map] = (state.activeKey || "@").split("@");
  return endpoint && endpoint.map === map &&
    String(endpoint.tier || "") === currentTierId();
}

function linkKindLabel(kind) {
  return {
    stairs: "楼梯",
    elevator: "电梯",
    portal: "传送",
    door: "门",
    rope: "绳索",
    other: "其他",
  }[kind] || kind;
}

// 连接箭头：单向只在终点一侧画，双向两端都画，一眼能分清能不能走回头路。
// 尺寸按**屏幕像素**恒定（所以除以 mapView.scale）——连接通常只有一两格宽，
// 缩放时跟着一起缩就看不见了。
const LINK_ARROW_PX = 8;         // 箭头长度
const LINK_ARROW_HALF = 4.5;     // 箭尾半宽
const LINK_ARROW_SHAFT_PX = 20;  // 短连接时双向标记的杆长（两个箭头尖的间距）
const LINK_ARROW_MIN_LINE = 26;  // 线短于此（屏幕像素）时改在中点画方向标记

function drawLinkArrowHead(g, tipX, tipY, dirX, dirY, color, px = LINK_ARROW_PX) {
  const scale = mapView.scale || 1;
  const size = px / scale;
  const half = (px * LINK_ARROW_HALF / LINK_ARROW_PX) / scale;
  const baseX = tipX - dirX * size;
  const baseY = tipY - dirY * size;
  g.beginPath();
  g.moveTo(tipX, tipY);
  g.lineTo(baseX - dirY * half, baseY + dirX * half);
  g.lineTo(baseX + dirY * half, baseY - dirX * half);
  g.closePath();
  g.fillStyle = color;
  g.fill();
}

function drawLinkArrows(g, from, to, color, bidirectional) {
  const dx = to[0] - from[0];
  const dy = to[1] - from[1];
  const len = Math.hypot(dx, dy);
  if (!len) return;
  const scale = mapView.scale || 1;
  const ux = dx / len;
  const uy = dy / len;
  if (len * scale >= LINK_ARROW_MIN_LINE) {
    // 够长：箭头贴在两端内侧，一端的头和两端的头隔着整条线，一眼能分清
    const inset = (LINK_ARROW_PX * 0.7) / scale;
    drawLinkArrowHead(g, to[0] - ux * inset, to[1] - uy * inset,
                      ux, uy, color);
    if (bidirectional) {
      drawLinkArrowHead(g, from[0] + ux * inset, from[1] + uy * inset,
                        -ux, -uy, color);
    }
    return;
  }
  // 短连接（一两格，绝大多数情况）：端点中心只隔几个像素，两个反向箭头会叠成
  // 一个菱形，单向双向看起来一模一样。所以不按线长画，改成在中点画固定尺寸的
  // 方向标记 —— 单向是一个实心箭头，双向是一条**两端带箭头的短线**（杆看得出来）。
  const mx = (from[0] + to[0]) / 2;
  const my = (from[1] + to[1]) / 2;
  const head = LINK_ARROW_PX / scale;
  const half = LINK_ARROW_SHAFT_PX / 2 / scale;
  const tipA = [mx + ux * half, my + uy * half];
  const tipB = [mx - ux * half, my - uy * half];
  g.save();
  g.setLineDash([]);
  if (bidirectional) {
    g.strokeStyle = color;
    g.lineWidth = 1.5 / scale;
    g.beginPath();
    g.moveTo(tipA[0], tipA[1]);
    g.lineTo(tipB[0], tipB[1]);
    g.stroke();
    drawLinkArrowHead(g, tipA[0], tipA[1], ux, uy, color, LINK_ARROW_PX);
    drawLinkArrowHead(g, tipB[0], tipB[1], -ux, -uy, color, LINK_ARROW_PX);
  } else {
    drawLinkArrowHead(g, mx + ux * (LINK_ARROW_PX * 0.5) / scale,
                      my + uy * (LINK_ARROW_PX * 0.5) / scale,
                      ux, uy, color, LINK_ARROW_PX);
  }
  g.restore();
}

function drawLinkLayer() {
  if (!linkCanvasEl || linkCanvasEl.width <= 1 || !calib.mapping) return;
  const vis = visibleRect();
  lctx.clearRect(vis.x, vis.y, vis.w, vis.h);
  if (!state.layers.links) return;
  const inv = calib.mapping.inverse_matrix;

  const pending = [];
  if (link.source) pending.push([link.source, "#76ff9f"]);
  if (link.target) pending.push([link.target, "#66baff"]);
  if (link.drag && link.drag.rect) {
    const e = ed();
    if (e) {
      const r = link.drag.rect;
      const rect = {
        x0: e.origin[0] + r.x0 * e.cellSize,
        z0: e.origin[1] + r.z0 * e.cellSize,
        x1: e.origin[0] + (r.x1 + 1) * e.cellSize,
        z1: e.origin[1] + (r.z1 + 1) * e.cellSize,
      };
      pending.push([{
        map: state.activeKey.split("@")[0],
        tier: currentTierId(),
        zoom: state.activeKey.split("@")[1] || "",
        rect,
      }, link.stage === "target" ? "#66baff" : "#76ff9f"]);
    }
  }

  for (const [endpoint, color] of pending) {
    drawWorldRect(lctx, endpoint.rect, inv,
      color + "33", color + "cc");
  }

  for (const item of state.gridLinks) {
    const fromCurrent = endpointIsCurrent(item.from);
    const toCurrent = endpointIsCurrent(item.to);
    if (!fromCurrent && !toCurrent) continue;
    // 端点悬空的连接规划器用不了，画成红色实心，别和可用连接一个样子
    const broken = !!(item.checks && !item.checks.valid);
    const fp = drawWorldRect(lctx, item.from.rect, inv,
      broken ? "rgba(220,68,52,0.30)" : (fromCurrent ? "rgba(118,255,159,0.24)" : "rgba(102,186,255,0.08)"),
      broken ? "rgba(220,68,52,0.95)" : (fromCurrent ? "rgba(118,255,159,0.9)" : "rgba(102,186,255,0.35)"));
    const tp = drawWorldRect(lctx, item.to.rect, inv,
      broken ? "rgba(220,68,52,0.30)" : (toCurrent ? "rgba(102,186,255,0.24)" : "rgba(118,255,159,0.08)"),
      broken ? "rgba(220,68,52,0.95)" : (toCurrent ? "rgba(102,186,255,0.9)" : "rgba(118,255,159,0.35)"));
    const fc = fp.reduce((a, p) => [a[0] + p[0] / 4, a[1] + p[1] / 4], [0, 0]);
    const tc = tp.reduce((a, p) => [a[0] + p[0] / 4, a[1] + p[1] / 4], [0, 0]);
    const rope = item.kind === "rope";
    const lineColor = broken
      ? "rgba(220,68,52,0.95)"
      : rope
        ? "rgba(255,150,64,0.96)"
        : "rgba(255,214,92,0.9)";
    lctx.save();
    lctx.setLineDash(broken
      ? [1 / mapView.scale, 4 / mapView.scale]
      : rope
        ? [2 / mapView.scale, 3 / mapView.scale]
        : [6 / mapView.scale, 4 / mapView.scale]);
    lctx.strokeStyle = lineColor;
    lctx.lineWidth = (rope ? 2 : 1.5) / mapView.scale;
    lctx.beginPath();
    lctx.moveTo(fc[0], fc[1]);
    lctx.lineTo(tc[0], tc[1]);
    lctx.stroke();
    if (rope && !broken) {
      lctx.setLineDash([]);
      lctx.beginPath();
      lctx.arc(tc[0], tc[1], 5 / mapView.scale, 0, Math.PI * 2);
      lctx.stroke();
      lctx.beginPath();
      lctx.arc(tc[0], tc[1], 2.5 / mapView.scale, 0, Math.PI * 2);
      lctx.fillStyle = "rgba(255,190,92,0.95)";
      lctx.fill();
    }
    // 箭头画在最后，压在虚线和绳索圆圈之上
    lctx.setLineDash([]);
    drawLinkArrows(lctx, fc, tc, lineColor, !!item.bidirectional);
    lctx.restore();
  }
}

// 端点框选时的即时校验。用**当前已载入的这一层**网格数格子：分层网格共用同一套
// origin/cell_size，而框这个端点时人正站在它所属的那层上，所以当前层的口径就是
// 这个端点的口径 —— 不用额外请求。
//
// 之前只在保存后才由服务端报「端点悬空」，等于让人先画错再被打回；实测那份数据里
// 7 条连接有 3 条就是这样来的。
function linkEndpointLocalReport(endpoint) {
  const e = ed();
  if (!e || !endpoint || !endpoint.rect) return null;
  const [map, zoom] = (state.activeKey || "@").split("@");
  if (endpoint.map !== map) return null;
  const zoomOf = String(endpoint.zoom || "");
  if (zoomOf && zoomOf !== zoom) return null;
  if (String(endpoint.tier || "") !== (currentTierId() || "")) return null;
  const cs = e.cellSize || 1;
  const range = {
    x0: Math.floor((endpoint.rect.x0 - e.origin[0]) / cs),
    x1: Math.ceil((endpoint.rect.x1 - e.origin[0]) / cs) - 1,
    z0: Math.floor((endpoint.rect.z0 - e.origin[1]) / cs),
    z1: Math.ceil((endpoint.rect.z1 - e.origin[1]) / cs) - 1,
  };
  let free = 0;
  let blocked = 0;
  e.forEachCell(range, (_ix, _iz, value) => {
    if (value === CELL_FREE) free++;
    else if (value === CELL_BLOCKED) blocked++;
  });
  const total = Math.max(0, (range.x1 - range.x0 + 1) * (range.z1 - range.z0 + 1));
  return { free, blocked, unknown: Math.max(0, total - free - blocked) };
}

// 当前是否框选着格子（网格模式的快捷键要靠它决定"做事"还是"选工具"）
function hasSelection() {
  const v = state.views.get(state.activeKey);
  return !!(v && v.edit && v.edit.selRect);
}

// 对选中的格子批量执行画笔操作
function applySelection(brush) {
  const v = state.views.get(state.activeKey);
  const e = v.edit;
  const rect = e && e.selRect;
  if (!rect) { addLog("没有选中的格子", "t-err"); return; }
  const total = (rect.x1 - rect.x0 + 1) * (rect.z1 - rect.z0 + 1);
  beginEditTransaction(e);
  const stateValue = brush === "free" ? CELL_FREE
    : brush === "blocked" ? CELL_BLOCKED
      : CELL_UNKNOWN;
  for (let ix = rect.x0; ix <= rect.x1; ix++) {
    for (let iz = rect.z0; iz <= rect.z1; iz++) {
      e.setCell(ix, iz, stateValue);
    }
  }
  commitEditTransaction(e);
  setGridDirty(true);
  e.selRect = null;
  updateEditStats();
  requestRender(RENDER_LAYERS.GRID | RENDER_LAYERS.SELECTION);
  scheduleFloorOverlapLiveRefresh();
  const label = brush === "free" ? "填充 Free" : brush === "blocked" ? "填充 Blocked" : "填充 未知";
  addLog("已对选中的 " + total + " 格执行: " + label, "t-ok");
  updateSelBar();
}

// 框选开始：以 (ax,az) 为锚点
function startMarquee(ix, iz, id) {
  edit.marquee = { id: id, ax: ix, az: iz, cur: [ix, iz] };
  requestRender(RENDER_LAYERS.SELECTION);
  updateSelBar();
}

function updateMarquee(ix, iz) {
  const m = edit.marquee;
  if (!m || (m.cur[0] === ix && m.cur[1] === iz)) return;
  m.cur = [ix, iz];
  requestRender(RENDER_LAYERS.SELECTION);      // rAF 合并：一次拖动最多每帧重画一次
  updateSelBar();
}

function finishMarquee() {
  const m = edit.marquee;
  const v = state.views.get(state.activeKey);
  edit.marquee = null;
  if (m && m.cur && v && v.edit) {
    const rect = { x0: Math.min(m.ax, m.cur[0]), x1: Math.max(m.ax, m.cur[0]),
                   z0: Math.min(m.az, m.cur[1]), z1: Math.max(m.az, m.cur[1]) };
    const total = (rect.x1 - rect.x0 + 1) * (rect.z1 - rect.z0 + 1);
    if (total <= 300000) {
      v.edit.selRect = rect;
      addLog("已框选 " + total + " 格，可填充 Free/Blocked/未知", "t-ok");
    } else {
      v.edit.selRect = null;
      addLog("框选区域过大（超过 30 万格），已取消", "t-err");
    }
  }
  requestRender(RENDER_LAYERS.SELECTION);
  updateSelBar();
}

// 编辑层不再常驻：退出编辑模式时缩到 1x1，避免又占一张地图尺寸的画布。
// 尺寸与摆放现在统一由 render() 里的 layoutLayer 负责。
function syncEditCanvas() {
  const v = state.views.get(state.activeKey);
  if ((!edit.mode && !link.mode) || !hasBounds(v)) {
    editCanvasEl.width = 1; editCanvasEl.height = 1;
    selCanvasEl.width = 1; selCanvasEl.height = 1;
    invalidateGridLayer();
  }
  requestRender(RENDER_LAYERS.GRID | RENDER_LAYERS.SELECTION);
}

function beginEditTransaction(e, withMeta = false) {
  e.beginTransaction(withMeta ? e.metadataSnapshot() : null);
}

function commitEditTransaction(e) {
  const active = e.history.active;
  const metaAfter = active && active.metaBefore ? e.metadataSnapshot() : null;
  return e.commitTransaction(metaAfter);
}

function refreshAfterGridHistory() {
  adoptViewEdit(edit.v);
  updateEditStats();
  updateSelBar();
  requestRender(RENDER_LAYERS.GRID | RENDER_LAYERS.SELECTION);
  scheduleFloorOverlapLiveRefresh();
}

function undoGridEdit() {
  const e = ed();
  if (!e) return false;
  if (!e.undo()) {
    setStatus("没有可撤销的网格操作", "idle");
    return false;
  }
  e.selRect = null;
  setGridDirty(true);
  refreshAfterGridHistory();
  setStatus("已撤销网格操作", "ready");
  return true;
}

function redoGridEdit() {
  const e = ed();
  if (!e) return false;
  if (!e.redo()) {
    setStatus("没有可重做的网格操作", "idle");
    return false;
  }
  e.selRect = null;
  setGridDirty(true);
  refreshAfterGridHistory();
  setStatus("已重做网格操作", "ready");
  return true;
}

// 画笔宽度：以目标格为中心的 w×w 方块（w=1 即单格）
function paintBlock(e, ix, iz, value) {
  const w = edit.brushSize || 1;
  const half = Math.floor((w - 1) / 2);
  let changed = false;
  for (let dx = -half; dx < w - half; dx++)
    for (let dy = -half; dy < w - half; dy++)
      if (e.setCell(ix + dx, iz + dy, value)) changed = true;
  return changed;
}

// 拖动涂色：从上一个格子到当前格子连一条线（Bresenham），每步按画笔宽度涂方块
function paintLineTo(ix, iz) {
  const e = ed();
  if (!e) return;
  const value = edit.brush === "free" ? CELL_FREE
    : edit.brush === "blocked" ? CELL_BLOCKED
      : CELL_UNKNOWN;
  if (!edit.lastCell) {
    if (paintBlock(e, ix, iz, value)) edit.paintDirty = true;
    edit.lastCell = [ix, iz];
    return;
  }
  let [lx, lz] = edit.lastCell;
  const dx = Math.abs(ix - lx), dy = Math.abs(iz - lz);
  const sx = ix > lx ? 1 : -1, sy = iz > lz ? 1 : -1;
  let err = dx - dy;
  let n = 0;
  while (n++ < 100000) {
    if (paintBlock(e, lx, lz, value)) edit.paintDirty = true;
    if (lx === ix && lz === iz) break;
    const e2 = 2 * err;
    if (e2 > -dy) { err -= dy; lx += sx; }
    if (e2 < dx) { err += dx; lz += sy; }
  }
  edit.lastCell = [ix, iz];
}

function updateEditStats() {
  const e = ed();
  if (!e) {
    $("editStats").textContent = "-";
    syncHistoryButtons();
    return;
  }
  // 未知格数 = 保存范围面积 − 已涂格数（范围内没涂色的都会被存成未知）
  const bb = savedBounds(e);
  const area = bb ? (bb.x1 - bb.x0 + 1) * (bb.z1 - bb.z0 + 1) : 0;
  const unknown = Math.max(0, area - e.size);
  $("editStats").textContent = "Free: " + e.freeCount + " / Blocked: " + e.blockedCount +
    (bb ? " / 未知: " + unknown + "　范围 " + (bb.x1 - bb.x0 + 1) + "×" + (bb.z1 - bb.z0 + 1)
        : "　（尚无格子）");
  syncHistoryButtons();
}

function setGridDirty(value) {
  edit.dirty = value;
  const chip = $("dirtyState");
  chip.dataset.dirty = String(value);
  chip.textContent = value ? "未保存" : "已保存";
}

function syncHistoryButtons() {
  let undo = false;
  let redo = false;
  if (edit.mode) {
    const history = ed() && ed().history;
    undo = !!(history && history.undo.length);
    redo = !!(history && history.redo.length);
  } else if (calib.mode) {
    undo = calibHistory.undo.length > 0;
    redo = calibHistory.redo.length > 0;
  }
  $("btnUndo").disabled = !undo;
  $("btnRedo").disabled = !redo;
}

function processPendingPaint() {
  const pending = edit.pendingPaint;
  edit.pendingPaint = null;
  if (!pending || !edit.painting || pending.id !== edit.painting.id) return false;
  const point = canvasPixelOfClient(pending.clientX, pending.clientY);
  if (!point) return false;
  const cell = cellAtPixel(point[0], point[1]);
  if (!cell) return false;
  paintLineTo(cell[0], cell[1]);
  return true;
}

function flushPaintFrame() {
  edit.paintFrameQueued = false;
  processPendingPaint();
  if (!edit.paintDirty) return;
  edit.paintDirty = false;
  setGridDirty(true);
  updateEditStats();
  render(RENDER_LAYERS.GRID);
  scheduleFloorOverlapLiveRefresh();
}

function queuePaintFrame() {
  if (edit.paintFrameQueued) return;
  edit.paintFrameQueued = true;
  requestAnimationFrame(flushPaintFrame);
}

function adoptViewEdit(v) {
  edit.v = v;
  const document = v.edit;
  $("editOriginX").value = document.origin[0];
  $("editOriginZ").value = document.origin[1];
  $("editCellSize").value = document.cellSize;
  updateEditStats();
  updateSelBar();
  syncEditCanvas();
}

// 载入该视图的 2D 网格（force=重新读文件）
async function loadGrid2D(force) {
  const wantKey = state.activeKey;
  const [map, zoom] = (state.activeKey || "").split("@");
  if (!map || !zoom) return;
  const v = state.views.get(state.activeKey) || getView(map, zoom);
  if (!v.edit) {
    v.edit = new GridDocument({ sourceTier: currentTierId() });
  }
  if (v.edit.loaded && !force) { adoptViewEdit(v); return; }
  const previousDocument = v.edit;
  try {
    const { document, response: res } = await loadGridDocument(
      map, zoom, currentTierId(),
    );
    if (document) {
      gridRasterCache.clear(previousDocument);
      v.edit = document;
      if (state.activeKey === wantKey) {
        $("editStatus").innerHTML = "已载入: <b>" + res.data.cells.length + "</b> Free / <b>" +
          res.data.blocked.length + "</b> Blocked" +
          (v.edit.shape ? "，保存范围 <b>" + v.edit.shape[0] + "×" + v.edit.shape[1] +
                          "</b> 格（含未涂色的未知格）" : "") +
          (res.source ? "<br>来源: <b>" + res.source + "</b>" : "") +
          (res.warnings && res.warnings.length
            ? '<br><span style="color:#ffcc55">⚠ ' + res.warnings.join("；") + '</span>' : "");
      }
    } else if (res.error) {
      if (state.activeKey === wantKey) {
        $("editStatus").innerHTML = '<span style="color:#ff9977">网格读取失败: ' + res.error + '</span>';
      }
    } else {
      gridRasterCache.clear(previousDocument);
      v.edit = new GridDocument({ sourceTier: currentTierId() });
      if (state.activeKey === wantKey) {
        $("editStatus").innerHTML = "无已存网格（origin=[0,0], cell_size=1；保存时按已涂格子的范围定 origin）";
      }
    }
    v.edit.loaded = true;
    invalidateGridLayer();
    // origin 可能变了，取坐标用的基准与统计都得跟着失效重算
    v.pickRef = null;
    v.pickRefPending = null;
  } catch (e) {
    if (state.activeKey === wantKey) {
      $("editStatus").textContent = "网格加载失败: " + e.message;
    }
  }
  v.edit.clearHistory();
  if (state.activeKey === wantKey) {
    adoptViewEdit(v);
    setGridDirty(false);
    if (edit.mode || link.mode) void loadFloorRegionMap(true);
    scheduleFloorOverlapLiveRefresh();
  }
}

function setEditMode(on) {
  if (on && (!calib.mapping || !calib.key || calib.key !== state.activeKey)) {
    setStatus("请先完成该地图的标定，才能在地图上放置格子", "warning");
    syncModeChrome();   // 被拒了，指示器要停在真实模式上
    return;
  }
  edit.mode = on;
  if (on) {
    if (pick.mode) setPickMode(false);
    if (link.mode) setLinkMode(false);
    setCalibMode(false);
    loadGrid2D(false);
    void loadFloorRegionMap(true);
    canvasWrap.style.cursor = "crosshair";
    setStatus("网格编辑：左键拖动涂色，Shift+左键框选，右键拖动平移，滚轮缩放", "active");
  } else {
    edit.painting = false;
    edit.marquee = null;
    hideBrushCursor();
    gridBoundsEl.style.display = "none";
    clearSelection();
    editCanvasEl.width = 1; editCanvasEl.height = 1;   // 清掉覆盖层
    selCanvasEl.width = 1; selCanvasEl.height = 1;
    invalidateGridLayer();
    canvasWrap.style.cursor = "";
    setStatus("已退出网格编辑");
  }
  syncModeChrome();
}

function endpointLabel(endpoint) {
  if (!endpoint) return "未选择";
  const zoom = String(endpoint.zoom || "");
  const [currentMap, currentZoom] = (state.activeKey || "@").split("@");
  // 端点属于别的 zoom 时把档次标出来，否则「大地图 → 大地图」这种看着像自环
  const suffix = zoom && (endpoint.map !== currentMap || zoom !== currentZoom)
    ? "@" + zoom : "";
  if (!endpoint.tier) {
    return `${mapDisplayName(endpoint.map)}${suffix} / 大地图`;
  }
  const layer = [...state.tierCatalogs.values()]
    .flat()
    .find(item => item.id === endpoint.tier);
  const tier = layer ? `${layer.level_name} · ${layer.name}` : endpoint.tier;
  return `${mapDisplayName(endpoint.map)}${suffix} / ${tier}`;
}

function endpointFromRect(rect) {
  const e = ed();
  const [map, zoom] = (state.activeKey || "@").split("@");
  if (!e || !map) return null;
  return {
    map,
    tier: currentTierId(),
    zoom: zoom || "",
    rect: {
      x0: e.origin[0] + rect.x0 * e.cellSize,
      z0: e.origin[1] + rect.z0 * e.cellSize,
      x1: e.origin[0] + (rect.x1 + 1) * e.cellSize,
      z1: e.origin[1] + (rect.z1 + 1) * e.cellSize,
    },
  };
}

function clearLinkDraft() {
  link.source = null;
  link.target = null;
  link.drag = null;
  link.editingId = null;
  link.stage = "source";
  updateLinkEditorUi();
  renderLinkList();
  requestRender(RENDER_LAYERS.LINK);
}

function setLinkStage(stage) {
  link.stage = stage === "target" ? "target" : "source";
  updateLinkEditorUi();
  requestRender(RENDER_LAYERS.LINK);
}

function updateLinkEditorUi() {
  $("btnLinkSource").classList.toggle("on", link.stage === "source");
  $("btnLinkTarget").classList.toggle("on", link.stage === "target");
  $("btnLinkSource").textContent = link.source ? "重选起点" : "选起点";
  $("btnLinkTarget").textContent = link.target ? "重选终点" : "选终点";
  $("btnLinkSave").disabled = !(link.source && link.target);
  const hint = $("linkEditStatus");
  let text = "起点 " + endpointLabel(link.source) +
    " · 终点 " + endpointLabel(link.target);
  // 框完就能看出这一端压在没涂过的格子上，不用等保存被拒
  const bad = [];
  for (const [endpoint, label] of
       [[link.source, "起点"], [link.target, "终点"]]) {
    const report = linkEndpointLocalReport(endpoint);
    if (!report) continue;
    if (!report.free) {
      bad.push(label + "没有可走格（Free 0 / 未知 " + report.unknown + "）");
    } else if (report.unknown) {
      text += " ｜ " + label + " Free " + report.free + " · 未知 " + report.unknown;
    }
  }
  if (bad.length) {
    hint.textContent = text + " ｜ " + bad.join("；") + "，保存会被拒绝";
    hint.classList.add("t-err");
  } else {
    hint.textContent = text;
    hint.classList.remove("t-err");
  }
}

function setLinkMode(on) {
  if (on && (!calib.mapping || !calib.key || calib.key !== state.activeKey)) {
    setStatus("请先完成该地图标定，才能编辑跨层连接", "warning");
    syncModeChrome();
    return;
  }
  link.mode = on;
  if (on) {
    if (edit.mode) setEditMode(false);
    if (pick.mode) setPickMode(false);
    setCalibMode(false);
    link.stage = "source";
    loadGrid2D(false);
    void loadFloorRegionMap(true);
    void loadGridLinks();
    canvasWrap.style.cursor = "crosshair";
    setStatus("连接模式：选择起点区域，切换分层后选择终点区域", "active");
  } else {
    link.drag = null;
    link.source = null;
    link.target = null;
    link.editingId = null;
    editCanvasEl.width = 1;
    editCanvasEl.height = 1;
    invalidateGridLayer();
    gridBoundsEl.style.display = "none";
    linkCanvasEl.width = 1;
    linkCanvasEl.height = 1;
    canvasWrap.style.cursor = "";
    updateLinkEditorUi();
    setStatus("已退出连接模式");
  }
  syncModeChrome();
}

async function loadGridLinks() {
  const [map] = (state.activeKey || "@").split("@");
  if (!map) return;
  try {
    const res = await fetch("/api/grid-links?map=" + encodeURIComponent(map)).then(r => r.json());
    state.gridLinks = res.ok ? (res.links || []) : [];
    const broken = state.gridLinks.filter(item => item.checks && !item.checks.valid);
    $("linkListStatus").textContent = res.ok
      ? (broken.length
          ? `本图 ${state.gridLinks.length} 条连接，其中 ${broken.length} 条端点无效（已标红，需重选端点）`
          : `本图 ${state.gridLinks.length} 条连接`)
      : ("读取失败: " + (res.error || "未知错误"));
    $("linkListStatus").classList.toggle("t-err", broken.length > 0);
  } catch (e) {
    state.gridLinks = [];
    $("linkListStatus").textContent = "读取失败: " + e.message;
  }
  renderLinkList();
  requestRender(RENDER_LAYERS.LINK);
  void loadNavReadiness();
}

// 「这张图现在能不能拿去导航」——就绪判定由服务端算（/api/nav-graph），这里只显示。
// 之前这个判定只有 curl 能看，属于做了功能却没接上界面。
async function loadNavReadiness() {
  const el = $("navReadyStatus");
  const list = $("navReadyList");
  if (!el || !list) return;
  const [map, zoom] = (state.activeKey || "@").split("@");
  if (!map || !zoom) {
    el.textContent = "分层导航：-";
    el.classList.remove("t-ok", "t-warn");
    list.innerHTML = "";
    return;
  }
  try {
    const res = await fetch(
      "/api/nav-graph?map=" + encodeURIComponent(map) +
      "&zoom=" + encodeURIComponent(zoom) + "&summary=1",
    ).then(r => r.json());
    if (!res.ok) {
      el.textContent = "分层导航：" + (res.error || "无法判定");
      el.classList.remove("t-ok");
      el.classList.add("t-warn");
      list.innerHTML = "";
      return;
    }
    const tiers = res.tiers || [];
    const counts = tiers
      .map(t => (t.tier_id === "__main__" ? "大地图" : t.tier_id) +
        " " + t.free_count + " 格")
      .join(" · ");
    if (res.ready) {
      el.innerHTML = '<b class="t-ok">已就绪</b> —— ' + tiers.length +
        " 层，" + counts;
      el.classList.remove("t-warn");
    } else {
      el.innerHTML = '<b class="t-warn">' + res.blockers.length +
        " 项待处理</b> —— " + tiers.length + " 层，" + counts;
      el.classList.remove("t-ok");
    }
    list.innerHTML = (res.blockers || [])
      .map(text => '<div class="nav-blocker">' + text + "</div>")
      .join("");
  } catch (e) {
    el.textContent = "分层导航：读取失败 " + e.message;
    list.innerHTML = "";
  }
}

function renderLinkList() {
  const box = $("linkList");
  if (!box) return;
  box.innerHTML = "";
  for (const item of state.gridLinks) {
    const invalid = !!(item.checks && !item.checks.valid);
    const editing = item.id === link.editingId;
    const row = document.createElement("div");
    row.className = "link-row" + (invalid ? " link-row-invalid" : "") +
      (editing ? " link-row-editing" : "");
    const text = document.createElement("div");
    text.innerHTML =
      "<b>" + endpointLabel(item.from) + "</b> → <b>" +
      endpointLabel(item.to) + "</b><br><small>" +
      linkKindLabel(item.kind) + " · " + (item.bidirectional ? "双向" : "单向") +
      " · cost " + item.cost +
      (invalid ? '<br><span class="t-err">端点无效：' +
        (item.checks.issue || "") + "</span>" : "") +
      "</small>";
    const edit = document.createElement("button");
    edit.textContent = editing ? "编辑中" : "编辑";
    if (editing) edit.classList.add("on");
    edit.onclick = () => void editGridLink(item);
    const del = document.createElement("button");
    del.textContent = "删除";
    del.onclick = () => void deleteGridLink(item.id);
    row.append(text, edit, del);
    box.appendChild(row);
  }
}

// 点“编辑”后把视图挪到这条连接上。两端可能不同地图/不同分层（编辑器一次只显示
// 一层），所以顺序是：切地图/层级 → 切分层 → 聚焦到两端范围。
async function focusGridLink(item) {
  const map = String(item.from.map || item.to.map || "");
  const zoom = String(item.from.zoom || item.to.zoom || "");
  if (!map) return false;
  const [curMap, curZoom] = (state.activeKey || "@").split("@");
  if (curMap !== map || (zoom && curZoom !== zoom)) {
    const targetZoom = syncMapSelectors(
      map, zoom || (curMap === map ? curZoom : ""));
    if (!targetZoom) {
      addLog("连接所在的地图不在本地列表中: " + map, "t-warn");
      return false;
    }
    if (state.activeKey !== viewKey(map, targetZoom)) {
      await openSelectedMap({ preserveFollow: true });
    }
    if (!(calib.mapping && calib.key === state.activeKey)) {
      await loadMappingForCurrent();
    }
    await loadTierCatalog();
  }

  // 已经站在这条连接的任一端所在分层时就不动它，别把人从当前层里拽走
  const current = currentTierId() || "";
  const fromTier = String(item.from.tier || "");
  const toTier = String(item.to.tier || "");
  const wantTier = (current === fromTier || current === toTier) ? current : fromTier;
  if (wantTier !== current) {
    await setActiveTier(wantTier);
    if ((currentTierId() || "") !== wantTier) {
      addLog("未能切到连接所在的分层，只聚焦到位置（先保存当前层网格再试）", "t-warn");
    }
  }

  // 两端离得远时 focusWorldBounds 只会放大到能看清的倍率、不会缩得比现在还小
  focusWorldBounds({
    x0: Math.min(Number(item.from.rect.x0), Number(item.to.rect.x0)),
    x1: Math.max(Number(item.from.rect.x1), Number(item.to.rect.x1)),
    z0: Math.min(Number(item.from.rect.z0), Number(item.to.rect.z0)),
    z1: Math.max(Number(item.from.rect.z1), Number(item.to.rect.z1)),
  });
  return true;
}

// 把已有连接读回编辑器：端点悬空的连接只能靠重选端点修，没有这个入口就只能
// 删掉重画。kind/双向/代价一并回填，避免修端点时把其它字段重置掉。
async function editGridLink(item) {
  if (!link.mode) setLinkMode(true);
  link.editingId = item.id;
  link.source = { ...item.from, rect: { ...item.from.rect } };
  link.target = { ...item.to, rect: { ...item.to.rect } };
  link.stage = "source";
  link.drag = null;
  if ($("linkKind")) $("linkKind").value = item.kind || "stairs";
  if ($("linkBidirectional")) {
    $("linkBidirectional").checked = !!item.bidirectional;
  }
  if ($("linkCost")) $("linkCost").value = item.cost;
  updateLinkEditorUi();
  requestRender(RENDER_LAYERS.LINK);
  renderLinkList();
  const broken = item.checks && !item.checks.valid;
  setStatus(
    broken
      ? "正在修改连接（端点无效）—— 切到对应分层后点“重选起点/重选终点”重新框选"
      : "正在修改连接 —— 点“重选起点/重选终点”可重新框选",
    broken ? "warning" : "active",
  );
  await focusGridLink(item);
}

async function saveGridLink() {
  if (!link.source || !link.target) return;
  const payload = {
    id: link.editingId || undefined,
    from: link.source,
    to: link.target,
    kind: $("linkKind").value,
    bidirectional: $("linkBidirectional").checked,
    cost: parseFloat($("linkCost").value) || 1,
  };
  try {
    const res = await fetch("/api/grid-links", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then(r => r.json());
    if (!res.ok) throw new Error(res.error || "保存失败");
    addLog("已保存分层连接: " + res.link.id, "t-ok");
    setStatus("已保存分层连接", "ok");
    link.source = null;
    link.target = null;
    link.editingId = null;
    link.stage = "source";
    await loadGridLinks();
    updateLinkEditorUi();
  } catch (e) {
    // 端点悬空是最常见的失败原因，光写日志容易看不见，状态栏也提示一次
    addLog("保存分层连接失败: " + e.message, "t-err");
    setStatus("保存分层连接失败: " + e.message, "error");
  }
}

async function deleteGridLink(linkId) {
  try {
    const res = await fetch("/api/grid-links/" + encodeURIComponent(linkId),
                            { method: "DELETE" }).then(r => r.json());
    if (!res.ok) throw new Error(res.error || "删除失败");
    await loadGridLinks();
  } catch (e) {
    addLog("删除分层连接失败: " + e.message, "t-err");
  }
}

// 可行走格的连通块与"孤立/被困"诊断都在 grid/walk-policy.js 里，按**命名的策略**
// 取判据。这里只负责把当前文档与保存范围递进去。
//
// 以前这两段各自内联了「什么叫可走」，分块告警那处用的是「节点认 Free、守卫只查
// Blocked」这组不匹配的判据，于是报出来的块数在产品默认策略下偏少（map02 报 11、
// 规划器看 12；base01 报 5、看 8）。抽出去之后判据只有一个来源。
function walkableComponents(e) {
  return walkableComponentsOf(e, savedBounds(e), WALK_POLICIES.noUnknown);
}

function walkableAnomalies(e) {
  return walkableAnomaliesOf(e, savedBounds(e));
}

// 保存后的连通性提示文案。只提示、不阻断。措辞必须留有余地：
// 分块只说明"不冒险到不了"，规划器本来就能靠穿未知格跨过去（代价更高）；
// 孤立格只说明"没有可行走邻居"，未必是误点；只有八邻全阻挡才算真被困。
function connectivityWarning(e) {
  const comps = walkableComponents(e);
  const { lone, trapped } = walkableAnomalies(e);
  const parts = [];
  if (comps.length > 1) {
    const total = comps.reduce((a, b) => a + b, 0);
    // 必须写明口径：块数取决于"要不要冒穿过未知格的险"。不写清楚，这个数字会被
    // 当成客观事实，而它只在产品默认（不穿未知格）下成立 —— 放宽口径时未知格变成
    // 可走，划分只会更粗、块数只会更少。
    parts.push("可行走分成 " + comps.length + " 块（" +
               policyLabel(WALK_POLICIES.noUnknown) + "），最大块 " +
               (comps[0] / total * 100).toFixed(1) +
               "%：不冒险到不了其它块，允许穿越未知格则块数更少");
  }
  if (lone.length) parts.push(lone.length + " 个可行走格没有可行走邻居（与主体不连通，确认无用可删）");
  if (trapped.length) parts.push(trapped.length + " 个可行走格八邻全是阻挡（真正被困，多半是误点）");
  return parts.join("；");
}

function unmarkedYellowComponents() {
  return (state.floorOverlap.components || []).filter(component =>
    component.severity === "xz_only" && !component.resolved
  );
}

function confirmUnmarkedYellowComponents(components) {
  return new Promise(resolve => {
    const dialog = $("overlapWarningDlg");
    const list = $("overlapWarningList");
    const names = components.slice(0, 8).map((component, index) =>
      "连通区 " + (index + 1) + "：" +
      component.cells.length + " 格，" +
      component.layer_names.join(" / ")
    );
    if (components.length > names.length) {
      names.push("另有 " + (components.length - names.length) + " 个未标注连通区");
    }
    list.innerHTML = names.map(text => "<div>" + text + "</div>").join("");
    dialog.style.display = "block";
    $("overlapWarningContinue").onclick = () => {
      dialog.style.display = "none";
      resolve(true);
    };
    $("overlapWarningCancel").onclick = () => {
      dialog.style.display = "none";
      resolve(false);
    };
  });
}

async function saveGrid2D() {
  const [map, zoom] = (state.activeKey || "").split("@");
  const e = ed();
  if (!map || !zoom || !e) { addLog("没有可保存的网格数据", "t-err"); return; }
  await loadFloorRegionMap(true);
  await loadFloorOverlaps(true);
  const unmarked = unmarkedYellowComponents();
  if (unmarked.length) {
    const proceed = await confirmUnmarkedYellowComponents(unmarked);
    if (!proceed) {
      setStatus("仍有 " + unmarked.length + " 个黄色连通区未标注高度", "warning");
      return;
    }
  }
  try {
    const res = await saveGridDocument(map, zoom, currentTierId(), e);
    if (!res.ok) { addLog("保存网格失败: " + res.error, "t-err"); return; }
    // 服务端按"原范围 ∪ 已涂格子"定最终范围，可能平移了下标基准；
    // 把内存模型同步过去，连续保存才不会漂移、也不会把范围缩回去
    const dx = res.shift ? res.shift[0] : 0, dz = res.shift ? res.shift[1] : 0;
    if (dx || dz) {
      e.rebase(dx, dz);
      e.selRect = null;
      gridRasterCache.clear(e);
      invalidateGridLayer();
    }
    if (res.origin) e.origin = [res.origin[0], res.origin[2]];
    if (res.shape) e.shape = [res.shape[0], res.shape[1]];
    e.clearHistory();
    setGridDirty(false);
    const cw = connectivityWarning(e);
    $("editStatus").innerHTML = "已保存: <b>" + res.path + "</b>" +
      (res.shape ? "（" + res.shape[0] + "×" + res.shape[1] + " 格）" : "") +
      (res.overwrote ? "（覆盖原文件）" : "（新建）") +
      (cw ? '<br><span style="color:#ffcc55">⚠ ' + cw + '</span>' : "");
    addLog("网格已保存: " + res.path, "t-ok");
    if (cw) addLog("连通性提醒: " + cw, "t-warn");
    updateEditStats();
    updateSelBar();
    requestRender(RENDER_LAYERS.GRID | RENDER_LAYERS.SELECTION);
    void loadNavReadiness();
  } catch (e) { addLog("保存失败: " + e.message, "t-err"); }
}

// ---------------- 路径标记（游戏坐标串 → 地图折线） ----------------
const pathCanvasEl = $("pathCanvas");
const pctx = pathCanvasEl.getContext("2d");

// 解析坐标串：优先 (x,z) 括号格式，fallback 裸 x,z；忽略 “->”、文字、标签等
function parsePathPoints(text) {
  const out = [];
  let m, re = /\((-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\)/g;
  while ((m = re.exec(text))) out.push({ x: parseFloat(m[1]), z: parseFloat(m[2]) });
  if (out.length) return out;
  re = /(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)/g;
  while ((m = re.exec(text))) out.push({ x: parseFloat(m[1]), z: parseFloat(m[2]) });
  return out;
}

// 世界坐标 (x,z) → 画布像素（用当前视图标定）
function worldToPixelLocal(x, z) {
  if (!calib.mapping || !calib.key || calib.key !== state.activeKey) return null;
  const inv = calib.mapping.inverse_matrix;
  return [inv[0][0] * x + inv[0][1] * z + inv[0][2],
          inv[1][0] * x + inv[1][1] * z + inv[1][2]];
}

function drawPath() {
  const vis = visibleRect();
  pctx.clearRect(vis.x, vis.y, vis.w, vis.h);
  const v = state.views.get(state.activeKey);
  if (!v || !v.path || !v.path.points || v.path.points.length < 2) return;
  const inv = calib.mapping && calib.key === state.activeKey ? calib.mapping.inverse_matrix : null;
  if (!inv) return;
  const pts = v.path.points.map(p =>
    [inv[0][0] * p.x + inv[0][1] * p.z + inv[0][2],
     inv[1][0] * p.x + inv[1][1] * p.z + inv[1][2]]);
  // 线宽/半径按 1/scale 给：画布是视口尺寸再被父层放大，
  // 不除 scale 的话放大后线会跟着变粗，缩小后细到看不见。
  const k = 1 / mapView.scale;
  // 折线
  pctx.strokeStyle = "rgba(255,170,60,0.95)";
  pctx.lineWidth = 3 * k; pctx.lineJoin = "round"; pctx.lineCap = "round";
  pctx.beginPath();
  pctx.moveTo(pts[0][0], pts[0][1]);
  for (let i = 1; i < pts.length; i++) pctx.lineTo(pts[i][0], pts[i][1]);
  pctx.stroke();
  // 起点/终点强调
  for (const idx of [0, pts.length - 1]) {
    pctx.fillStyle = idx === 0 ? "#66ff99" : "#ff6666";
    pctx.beginPath(); pctx.arc(pts[idx][0], pts[idx][1], 6 * k, 0, Math.PI * 2); pctx.fill();
    pctx.strokeStyle = "#fff"; pctx.lineWidth = 1.5 * k; pctx.stroke();
  }
  // 途经点 + 编号
  pts.forEach((p, i) => {
    pctx.fillStyle = "#ffd27a";
    pctx.beginPath(); pctx.arc(p[0], p[1], 4 * k, 0, Math.PI * 2); pctx.fill();
    pctx.strokeStyle = "#7a4a00"; pctx.lineWidth = 1 * k; pctx.stroke();
    pctx.fillStyle = "#fff";
    pctx.font = (11 * k) + "px Consolas";
    pctx.fillText(String(i + 1), p[0] + 7 * k, p[1] - 7 * k);
  });
}

// 路径层由 render() 统一摆放，这里只要触发一次重画
function syncPathCanvas() {
  requestRender(RENDER_LAYERS.PATH);
}

function markPath() {
  const v = state.views.get(state.activeKey);
  if (!v) { $("pathStatus").textContent = "请先选择一张地图"; return; }
  if (!worldToPixelLocal(0, 0)) {
    $("pathStatus").textContent = "该地图尚未标定，先完成标定才能标记路径";
    return;
  }
  const pts = parsePathPoints($("pathInput").value);
  if (pts.length < 2) {
    $("pathStatus").textContent = "解析不到坐标点（需要至少 2 个点），检查输入格式";
    return;
  }
  v.path = { points: pts };
  syncPathCanvas();
  $("pathStatus").innerHTML = "已标记 <b>" + pts.length + "</b> 个点：(" + pts[0].x + "," +
    pts[0].z + ") → (" + pts[pts.length - 1].x + "," + pts[pts.length - 1].z + ")";
  addLog("路径已标记 " + pts.length + " 点", "t-ok");
}

function clearPath() {
  const v = state.views.get(state.activeKey);
  if (v) v.path = null;
  pctx.clearRect(0, 0, pathCanvasEl.width, pathCanvasEl.height);
  $("pathStatus").textContent = "路径已清除";
}

// ---------------- 取坐标（点击地图显示游戏坐标/格子坐标） ----------------
const pick = { mode: false };

function hidePickMarker() {
  $("pickMarker").style.display = "none";
}

function setPickMode(on) {
  if (on) {
    if (link.mode) setLinkMode(false);
    setCalibMode(false);
    setEditMode(false);
  }
  pick.mode = on;
  canvasWrap.style.cursor = on ? "crosshair" : "";
  setStatus(on
    ? "取坐标模式：点击地图任意位置显示该点游戏坐标（左键拖动平移 · 滚轮缩放）"
    : "已退出取坐标模式", on ? "active" : "idle");
  if (!on) {
    hidePickMarker();
    $("pickReadout").textContent = "-";
  }
  syncModeChrome();
}

// 懒加载该视图的 2D 网格基准（格子坐标换算用）
async function ensurePickRef() {
  const v = state.views.get(state.activeKey);
  if (!v) return null;
  if (v.pickRef) return v.pickRef;
  if (v.pickRefPending) return v.pickRefPending;
  v.pickRefPending = (async () => {
    try {
      const [map, zoom] = (state.activeKey || "").split("@");
      const resp = await fetch(
        "/api/grid2d?map=" + encodeURIComponent(map) +
        "&zoom=" + encodeURIComponent(zoom) +
        "&tier=" + encodeURIComponent(currentTierId()),
      );
      const res = await resp.json();
      if (res.data && res.data.origin && res.data.cell_size) {
        v.pickRef = { origin: [res.data.origin[0], res.data.origin[2]], cellSize: res.data.cell_size };
        return v.pickRef;
      }
      return null;
    } catch (e) { return null; }
    finally { v.pickRefPending = null; }
  })();
  return v.pickRefPending;
}

async function doPick(e) {
  const p = canvasPixelOfEvent(e);
  if (!p) return;
  const px = p[0], py = p[1];
  const mk = $("pickMarker");
  const f1 = n => n.toFixed(2);
  if (calib.mapping && calib.key === state.activeKey) {
    // 像素 → 游戏世界坐标（用标定矩阵）
    const m = calib.mapping.matrix;
    const wx = m[0][0] * px + m[0][1] * py + m[0][2];
    const wz = m[1][0] * px + m[1][1] * py + m[1][2];
    mk.style.left = px + "px"; mk.style.top = py + "px";
    mk.dataset.label = "(" + f1(wx) + ", " + f1(wz) + ")";
    mk.style.display = "block";
    const ref = await ensurePickRef();
    if (ref) {
      const ix = Math.floor((wx - ref.origin[0]) / ref.cellSize);
      const iz = Math.floor((wz - ref.origin[1]) / ref.cellSize);
      $("pickReadout").textContent = "(" + f1(wx) + ", " + f1(wz) + ") ｜ 格子 [" + ix + ", " + iz + "]";
    } else {
      $("pickReadout").textContent = "(" + f1(wx) + ", " + f1(wz) + ")";
    }
    setStatus("坐标: (" + f1(wx) + ", " + f1(wz) + ")", "ready");
  } else {
    $("pickReadout").textContent = "未标定：像素 (" + Math.round(px) + ", " + Math.round(py) + ")";
    setStatus("该地图未标定，仅显示像素坐标", "warning");
  }
}

// ---------------- 日志 / 统计 ----------------
// 注意：正文必须按**文本**插入，不能用 innerHTML。服务端传来的错误串可能是
// '<urlopen error ...>' 这种尖括号包起来的形状，当 HTML 解析会被浏览器整段吃掉，
// 日志里只剩"标记抓取失败:"，把真正的原因吞掉（2026-09-13 排查 WAF 掐连接时踩到）。
function addLog(text, cls) {
  const d = document.createElement("div");
  const ts = document.createElement("span");
  ts.className = "t-time";
  ts.textContent = new Date().toLocaleTimeString();
  d.appendChild(ts);
  d.appendChild(document.createTextNode(text));
  if (cls) d.className = cls;
  logBox.appendChild(d);
  while (logBox.children.length > 400) logBox.removeChild(logBox.firstChild);
  logBox.scrollTop = logBox.scrollHeight;
}

function updateCounts() {
  $("cNew").textContent = state.counts.new;
  $("cUpdated").textContent = state.counts.updated;
  $("cUnchanged").textContent = state.counts.unchanged;
  $("cSkipped").textContent = state.counts.skipped;
  $("cError").textContent = state.counts.error;
}

// 抓取与合成是两件独立的事，按钮状态必须从两个标志分别推出来。
// 以前合成结束时无条件 setBusyFetch(false)，会把仍在运行的抓取状态一起清掉：
// 「停止抓取」被禁用、「开始抓取」被启用，而服务端只会回「抓取已在运行中」，
// 用户再也停不下那个抓取。
function syncButtons() {
  $("btnFetch").disabled = state.busyFetch;
  $("btnFetchStop").disabled = !state.busyFetch;
  $("btnSim").disabled = state.busyFetch || !!state.compose;
  // 抓取状态用状态点表达，不用按钮的绿/红填充——那个和"网格"模式的绿会撞车
  $("fetchDot").classList.toggle("on", state.busyFetch);
  $("fetchState").textContent = state.busyFetch ? "抓取中" : "未抓取";
}

// ---------------- 地图标记抓取 ----------------
// 与命令行 `nav-grid-editor fetch-marks` 共用同一份逻辑（services.marks），
// 这里只是换个触发方式，产出与统计口径一致。
async function loadMarksStatus() {
  try {
    state.marks = await (await fetch("/api/marks/status")).json();
  } catch (e) { /* 拿不到就保持原样，不打断页面 */ }
  renderMarksPanel();
}

function renderMarksPanel() {
  const m = state.marks;
  if (!m) return;
  $("marksCred").innerHTML = m.credential_ready
    ? '凭证: <b>已就位</b> <span class="t-time">' + m.credential_file + "</span>"
    : '凭证: <b class="t-warn">未找到</b> —— 认证口径需要把 hg/check 的 data.content '
      + "粘到 <span class=\"t-time\">" + m.credential_file + "</span>（该文件已 gitignore）";

  const s = m.stats;
  let line;
  if (m.running) line = "抓取中…（进度见日志）";
  else if (m.error) line = '<b class="t-err">上次失败: ' + m.error + "</b>";
  else if (s) {
    line = "上次: " + s.maps + " 图 / " + s.items + " 物品名 / " + s.points + " 点位";
    if (s.structure_points) line += " · 玩家自建结构 " + s.structure_points + " 个";
    line += '<br><span class="t-time">' + s.out_dir + "</span>";
  } else line = "未抓取";
  $("marksStatus").innerHTML = line;

  $("btnMarksAuth").disabled = !!m.running;
}

async function startMarks() {
  try {
    const res = await (await fetch("/api/marks/fetch", { method: "POST" })).json();
    if (!res.ok) { addLog("标记抓取启动失败: " + res.error, "t-err"); return; }
    addLog("开始抓取地图标记（认证口径）", "t-ok");
    state.marks = Object.assign({}, state.marks,
      { running: true, stats: null, error: null });
  } catch (e) {
    addLog("标记抓取请求失败: " + e.message, "t-err");
  }
  renderMarksPanel();
}

// ---------------- 玩家位置流（官方地图 WS） ----------------
// 服务端直接连 wss://ws.skland.com/.../endfield/map，这里只读状态、控制启停。
function syncMapSelectors(mapId, preferredZoom = "") {
  const zooms = (state.tileMaps[mapId] || []).slice().sort((a, b) => +a - +b);
  const selM = $("selMap"), selZ = $("selZoom");
  if (!zooms.length || !selM || !selZ) return null;
  selM.value = mapId;
  selZ.innerHTML = "";
  for (const zoom of zooms) {
    const option = document.createElement("option");
    option.value = zoom;
    option.textContent = zoom;
    selZ.appendChild(option);
  }
  const wanted = preferredZoom && zooms.includes(String(preferredZoom))
    ? String(preferredZoom)
    : String(zooms[zooms.length - 1]);
  selZ.value = wanted;
  return wanted;
}

async function syncPositionLocation(data) {
  if (!data || typeof data !== "object") return;
  const mapId = String(data.mapId || "").trim();
  const levelId = String(data.levelId || "").trim();
  if (!mapId) return;
  const locationKey = mapId + "|" + levelId;
  if (locationKey === state.positionView.appliedKey) return;

  const currentKey = state.activeKey || "";
  const currentMap = currentKey.split("@")[0] || "";
  if (currentMap !== mapId && (edit.mode || link.mode) && edit.dirty) {
    setStatus("实时位置已进入 " + mapDisplayName(mapId) +
              "，请先保存当前网格再自动切换地图", "warning");
    state.positionView.appliedKey = locationKey;
    return;
  }

  const requestId = ++state.positionView.requestId;
  const currentZoom = currentKey.split("@")[1] || "";
  const zoom = syncMapSelectors(
    mapId, currentMap === mapId ? currentZoom : "",
  );
  if (!zoom) {
    addLog("实时位置地图不在本地列表中: " + mapId, "t-warn");
    return;
  }

  const key = viewKey(mapId, zoom);
  if (state.activeKey !== key) {
    await openSelectedMap({ preserveFollow: true });
  }
  if (requestId !== state.positionView.requestId || state.activeKey !== key) return;

  if (!(calib.mapping && calib.key === key)) {
    await loadMappingForCurrent();
  }
  if (requestId !== state.positionView.requestId || state.activeKey !== key) return;

  await loadTierCatalog();
  if (requestId !== state.positionView.requestId || state.activeKey !== key) return;

  const activeTierId = currentTierId();
  const floorStatus = String(data.gridFloorStatus || "");
  let targetTierId = activeTierId;
  if (floorStatus === "unique") {
    targetTierId = String(data.gridTierId || "");
  } else if (floorStatus === "main") {
    targetTierId = "";
  } else if (floorStatus === "ambiguous") {
    addLog("WS 三维位置命中多个网格楼层，保持当前楼层不自动切换", "t-warn");
    state.positionView.appliedKey = locationKey;
    return;
  } else if (floorStatus === "no_grid" || floorStatus === "no_match") {
    addLog("当前区域没有可匹配的网格高度区间，保持当前楼层", "t-warn");
    state.positionView.appliedKey = locationKey;
    return;
  }

  if (targetTierId && !state.layers.tier) {
    state.layers.tier = true;
    $("layerTier").checked = true;
    requestRender(RENDER_LAYERS.BASE);
  }
  if (targetTierId !== activeTierId) {
    await setActiveTier(targetTierId);
  }
  if (requestId !== state.positionView.requestId || state.activeKey !== key) return;
  state.positionView.appliedKey = locationKey;
  updatePlayerMarker(data.pos || data);
}

async function loadPositionStatus() {
  try {
    state.position = await (await fetch("/api/position/status")).json();
  } catch (e) { /* 拿不到就保持原样，不打断页面 */ }
  renderPositionPanel();
}

function renderPositionPanel() {
  const p = state.position;
  const el = $("posStreamState");
  if (!p || !el) return;
  let line;
  if (p.connected) {
    line = '位置流: <b class="t-ok">已连接</b>';
    if (p.last_position) {
      const q = p.last_position;
      line += ' <span class="t-time">(' + q.x.toFixed(1) + ", " + q.z.toFixed(1) + ")</span>";
    }
  } else if (!p.credential_ready) {
    line = '位置流: <b class="t-warn">缺凭证</b> —— 需要 configs/hg_content.txt';
  } else if (p.running) {
    line = "位置流: 连接中…";
  } else {
    line = "位置流: 未开启";
  }
  if (!p.connected && p.last_error) {
    line += '<br><span class="t-err">' + p.last_error + "</span>";
  }
  el.innerHTML = line;
  const btn = $("btnPosToggle");
  if (btn) btn.textContent = p.running ? "停止位置流" : "开启位置流";
  const chk = $("chkWs");
  if (chk) chk.checked = !!p.running;
  const dot = $("wsDot");
  if (dot) dot.classList.toggle("on", !!p.connected);
}

async function togglePositionStream() {
  const on = !(state.position && state.position.running);
  if (on) state.positionView.appliedKey = "";
  try {
    const res = await (await fetch("/api/position/" + (on ? "start" : "stop"),
      { method: "POST" })).json();
    if (!res.ok) addLog("位置流操作失败: " + (res.error || "未知错误"), "t-err");
  } catch (e) {
    addLog("位置流请求失败: " + e.message, "t-err");
  }
  await loadPositionStatus();
}

// ---------------- 标记图层（把图标画到地图上） ----------------
// 标记是世界坐标 (x,y,z)，要靠标定矩阵换算成像素；没标定的图没法画。
// 跟瓦片一样"只画可见范围 + 设缩放下限"——map01@4 有 2414 个点，不设限会糊满屏。
const markIcons = new Map();      // 图标文件名 -> Image | "loading" | "missing"
const markBadges = new Map();     // 图标文件名 -> 合成好的徽章画布
const MARK_MIN_SCALE = 0.12;      // 低于此缩放不画（与瓦片的 600 张阈值同量级）
const MARK_BADGE_PX = 40;         // 徽章直径（**屏幕像素**，与缩放无关）
const MARK_POINTER_PX = 8;        // 底部小三角高度（屏幕像素）
const MARK_BUCKET_SIZE = 256;     // 世界坐标空间索引格子
const DEFAULT_MARK_MAIN_TYPE = "地图探索";
const DEFAULT_MARK_NAMES = new Set(["协议传送点", "次级核心", "协议核心", "分层入口"]);
const LAYER_ENTRANCE_TID = "feec2fbd748b3d10131a07971b25baf2";

function showMarks() { return !!(state.markers && state.markers.points.length); }

function defaultHiddenMarkTids(markers) {
  // base01 保持完整点位；其余地图默认只显示地图探索里的指定三类。
  if ((markers.key || "").split("@")[0] === "base01") return new Set();
  const present = new Set((markers.points || []).map(p => p[0]));
  const hidden = new Set();
  for (const [tid, info] of Object.entries(markers.templates || {})) {
    if (!present.has(tid)) continue;
    if (info.m === DEFAULT_MARK_MAIN_TYPE && DEFAULT_MARK_NAMES.has(info.n)) continue;
    hidden.add(tid);
  }
  return hidden;
}

function buildMarkerIndex(points) {
  const buckets = new Map();
  for (const point of points || []) {
    const cx = Math.floor(Number(point[1]) / MARK_BUCKET_SIZE);
    const cz = Math.floor(Number(point[3]) / MARK_BUCKET_SIZE);
    const key = cx + "," + cz;
    let bucket = buckets.get(key);
    if (!bucket) {
      bucket = [];
      buckets.set(key, bucket);
    }
    bucket.push(point);
  }
  return buckets;
}

function markerCandidates(vis, matrix) {
  const markerData = state.markers;
  if (!markerData || !markerData.index || !matrix) return markerData ? markerData.points : [];
  const corners = [
    [vis.x, vis.y],
    [vis.x + vis.w, vis.y],
    [vis.x, vis.y + vis.h],
    [vis.x + vis.w, vis.y + vis.h],
  ].map(([px, py]) => [
    matrix[0][0] * px + matrix[0][1] * py + matrix[0][2],
    matrix[1][0] * px + matrix[1][1] * py + matrix[1][2],
  ]);
  const minX = Math.min(...corners.map(item => item[0]));
  const maxX = Math.max(...corners.map(item => item[0]));
  const minZ = Math.min(...corners.map(item => item[1]));
  const maxZ = Math.max(...corners.map(item => item[1]));
  const cx0 = Math.floor(minX / MARK_BUCKET_SIZE) - 1;
  const cx1 = Math.floor(maxX / MARK_BUCKET_SIZE) + 1;
  const cz0 = Math.floor(minZ / MARK_BUCKET_SIZE) - 1;
  const cz1 = Math.floor(maxZ / MARK_BUCKET_SIZE) + 1;
  if ((cx1 - cx0 + 1) * (cz1 - cz0 + 1) > 4096) return markerData.points;
  const out = [];
  for (let cz = cz0; cz <= cz1; cz++) {
    for (let cx = cx0; cx <= cx1; cx++) {
      const bucket = markerData.index.get(cx + "," + cz);
      if (bucket) out.push(...bucket);
    }
  }
  return out;
}

// 图标以**中文名**落盘（如「供电设备.png」），多个 templateId 可能指向同一个文件，
// 所以按文件名缓存而不是按 templateId。
function markIcon(file) {
  const cur = markIcons.get(file);
  if (cur !== undefined) return cur;
  markIcons.set(file, "loading");
  const img = new Image();
  img.onload = () => { markIcons.set(file, img); requestRender(RENDER_LAYERS.BASE); };
  img.onerror = () => { markIcons.set(file, "missing"); };
  img.src = "/api/marks/icon?f=" + encodeURIComponent(file);
  return "loading";
}

// 徽章 = 深色圆底 + 白色图标，按**设备像素**预先合成一张离屏画布（每张图只合一次）。
// 合成时按 DPR 出图、绘制时再缩到"屏幕恒定大小"，所以放大缩小都不会糊也不会变大小。
function markBadge(file) {
  const cached = markBadges.get(file);
  if (cached !== undefined) return cached;
  const img = markIcon(file);
  if (!(img instanceof Image)) return null;      // 还在下载或失败了
  const px = Math.max(8, Math.round(MARK_BADGE_PX * DPR));
  const cv = document.createElement("canvas");
  cv.width = cv.height = px;
  const c = cv.getContext("2d");
  const r = px / 2;
  // 底色用**径向渐变**（左上偏亮、右下偏暗）而不是平涂——参考图里那种圆底。
  // 半透明（能隐约透出底图）但整体偏实，且**不描边**：白色描边会把图标框住。
  const grad = c.createRadialGradient(px * 0.36, px * 0.30, px * 0.05, r, r, r);
  grad.addColorStop(0, "rgba(62,62,76,0.82)");
  grad.addColorStop(1, "rgba(12,12,18,0.82)");
  c.beginPath();
  c.arc(r, r, r, 0, Math.PI * 2);
  c.fillStyle = grad;
  c.fill();
  const inner = px * 0.72;                       // 图标占圆的比例（原来 0.58 偏小）
  c.drawImage(img, (px - inner) / 2, (px - inner) / 2, inner, inner);
  markBadges.set(file, cv);
  return cv;
}

// 并发调用**共享同一次请求**：以前是"key 相同就 return"，于是第二个调用者
// 立刻返回、看到的却是上一个调用刚设下的 loaded:false，直接放弃——
// 换图时标记就画不出来。
async function loadMarkers() {
  const key = state.activeKey;
  if (!key) return;
  if (state.markers && state.markers.loaded && state.markers.key === key) return;
  if (state.markersPromise && state.markersKey === key) return state.markersPromise;
  const map = key.split("@")[0];
  state.markersKey = key;
  state.markers = { key, templates: {}, points: [], loaded: false };
  state.markersPromise = (async () => {
    try {
      const res = await (await fetch("/api/marks/data?map=" + encodeURIComponent(map))).json();
      if (state.activeKey !== key) return;     // 期间切图了，丢弃
      state.markers = { key, templates: res.templates || {},
                        points: res.points || [], loaded: true,
                        index: buildMarkerIndex(res.points || []) };
      state.markHidden = defaultHiddenMarkTids(state.markers);
    } catch (e) {
      addLog("标记数据加载失败: " + e.message, "t-warn");
    } finally {
      state.markersPromise = null;
    }
    renderMarksPanel();
    buildMarkPicker();   // 数据到位后才填筛选器（失败时它会显示占位文案）
    updateMarksHint();
    requestRender(RENDER_LAYERS.BASE);
  })();
  return state.markersPromise;
}

function drawMarkers(g) {
  // 标定模式只保留控制点与底图，避免标记徽章干扰特征点选取。
  if (calib.mode) return;
  if (!showMarks()) return;
  const s = mapView.scale;
  if (s < MARK_MIN_SCALE) return;            // 缩得太小就不画（跟瓦片一样）
  const vis = visibleRect();
  const v = state.views.get(state.activeKey);
  if (!v || !hasBounds(v)) return;
  const m = state.markers;
  const inv = (calib.mapping && calib.key === state.activeKey)
    ? calib.mapping.inverse_matrix : null;
  if (!inv) return;                          // 没标定就拿不到 世界→像素
  const matrix = calib.mapping.matrix;

  // 1/scale 把"屏幕像素"折算成 holder 单位：画布整体被缩放 s，holder 尺寸 × s
  // 才是屏幕尺寸。这样无论缩放多少，徽章在屏幕上都一样大。
  const k = 1 / s;
  const d = MARK_BADGE_PX * k;               // 圆直径
  const ph = MARK_POINTER_PX * k;            // 三角高
  const pw = ph * 0.85;
  const padTop = d + ph;                     // 剔除时留出整个徽章的高度（往上长）

  for (const [tid, wx, _wy, wz] of markerCandidates(vis, matrix)) {
    const info = m.templates[tid];
    if (!info || state.markHidden.has(tid)) continue;   // 选择器里逐个关掉的
    const px = inv[0][0] * wx + inv[0][1] * wz + inv[0][2];
    const py = inv[1][0] * wx + inv[1][1] * wz + inv[1][2];
    if (px < vis.x - d || px > vis.x + vis.w + d) continue;
    if (py < vis.y - padTop || py > vis.y + vis.h + d) continue;

    const badge = info.icon ? markBadge(info.icon) : null;
    if (badge) {
      // 底部小三角的**尖端正好落在锚点**上，圆徽章坐在它上面
      g.beginPath();
      g.moveTo(px - pw, py - ph);
      g.lineTo(px + pw, py - ph);
      g.lineTo(px, py);
      g.closePath();
      g.fillStyle = "#ffcc33";
      g.fill();
      g.drawImage(badge, px - d / 2, py - ph - d, d, d);
    } else {
      // 没有图标（或还没下载完）就画个小圆点，锚点仍是那个位置
      g.beginPath();
      g.arc(px, py - 4 * k, Math.max(1.5, 4 * k), 0, Math.PI * 2);
      g.fillStyle = "rgba(255,209,102,0.85)";
      g.fill();
    }
  }
}

// ---------------- 物品图标选择器 ----------------
// 常驻在侧栏的面板：按 catalog 大类列出本图出现的图标，点图标逐个筛掉它
// （见 toggleMarkItem）。由 loadMarkers() 在数据到位后填充。

// 归出 {大类: [{tid, 名称, 图标文件, 本图点位数}]}，只保留**本图真有点位**的图标
function buildMarkPicker() {
  const m = state.markers;
  if (!m || !m.loaded) {
    state.mpCats = []; state.mpCat = null;
    $("mpSub").textContent = "-";
    $("mpCats").innerHTML = "";
    $("mpItems").innerHTML = '<div class="mp-empty">' +
      (m ? "标记数据加载中…" : "先在上面选一张地图") + "</div>";
    return;
  }
  const tpls = m.templates;
  const cnt = new Map();
  for (const p of m.points) cnt.set(p[0], (cnt.get(p[0]) || 0) + 1);

  const byCat = new Map();
  for (const [tid, info] of Object.entries(tpls)) {
    const n = cnt.get(tid) || 0;
    if (!n || !info.m) continue;
    if (!byCat.has(info.m)) byCat.set(info.m, []);
    byCat.get(info.m).push({ tid, name: info.n || "?", icon: info.icon || "", n });
  }
  for (const arr of byCat.values()) {
    arr.sort((a, b) => b.n - a.n || a.name.localeCompare(b.name, "zh"));
  }
  state.mpCats = [...byCat.entries()].sort((a, b) => a[0].localeCompare(b[0], "zh"));
  $("mpSub").textContent = `本图 ${state.markers.points.length} 个点位`;

  if (!state.mpCats.length) {
    state.mpCat = null;
    $("mpCats").innerHTML = "";
    $("mpItems").innerHTML = '<div class="mp-empty">这张图没有标记数据（先去抓一次）</div>';
    return;
  }
  if (!state.mpCats.some(([c]) => c === state.mpCat)) {
    const hasDefault = state.mpCats.some(([c]) => c === DEFAULT_MARK_MAIN_TYPE);
    state.mpCat = hasDefault ? DEFAULT_MARK_MAIN_TYPE : state.mpCats[0][0];
  }
  renderMpCats();
  renderMpItems();
}

function renderMpCats() {
  $("mpCats").innerHTML = state.mpCats.map(([cat, items]) => {
    const n = items.length;
    return '<button class="mp-cat' + (cat === state.mpCat ? " on" : "") +
           '" data-cat="' + cat + '"><span>' + cat + '</span>' +
           '<span class="mp-n">' + n + "</span></button>";
  }).join("");
  $("mpCats").querySelectorAll(".mp-cat").forEach(b => {
    b.onclick = () => { state.mpCat = b.dataset.cat; renderMpCats(); renderMpItems(); };
  });
}

function renderMpItems() {
  const entry = state.mpCats.find(([c]) => c === state.mpCat);
  const box = $("mpItems");
  if (!entry) { box.innerHTML = ""; return; }
  box.innerHTML = entry[1].map(it => {
    const off = state.markHidden.has(it.tid);
    return '<div class="mp-item' + (off ? " off" : "") +
      '" data-tid="' + it.tid + '" title="' + it.name + "（" + it.n +
      " 个点，点击" + (off ? "显示" : "隐藏") + "）\">" +
      (it.icon
        ? '<img src="/api/marks/icon?f=' + encodeURIComponent(it.icon) + '" alt="">'
        : '<div class="mp-ph"></div>') +
      '<div class="mp-cnt">' + it.n + "</div>" +
      '<div class="mp-name">' + it.name + "</div></div>";
  }).join("");
  box.querySelectorAll(".mp-item").forEach(el => {
    el.onclick = () => toggleMarkItem(el.dataset.tid);
  });
}

// 点图标 = **筛掉/放回**这个物品在地图上的显示；不跳转视图
function toggleMarkItem(tid) {
  if (state.markHidden.has(tid)) state.markHidden.delete(tid);
  else state.markHidden.add(tid);
  renderMpItems();
  updateMarksHint();
  requestRender(RENDER_LAYERS.BASE);
}

// 全选 / 取消全选：作用在**整张图的所有图标**上（不只是当前这个大类的）
function setAllMarkItems(visible) {
  if (!state.markers) return;
  const tids = new Set(state.markers.points.map(p => p[0]));
  if (visible) state.markHidden.clear();
  else { state.markHidden.clear(); for (const t of tids) state.markHidden.add(t); }
  renderMpItems();
  updateMarksHint();
  requestRender(RENDER_LAYERS.BASE);
}

function resetMarkFilter() {
  state.markHidden = state.markers
    ? defaultHiddenMarkTids(state.markers)
    : new Set();
  renderMpItems();
  updateMarksHint();
  requestRender(RENDER_LAYERS.BASE);
}

function updateMarksHint() {
  const el = $("marksHint");
  const m = state.markers;
  if (!m) { el.textContent = ""; return; }        // 还没选图，别写"加载中"
  if (!m.loaded) { el.textContent = "加载中…"; return; }
  if (!m.points.length) {
    el.textContent = "这张图没有标记数据（先去上面抓一次）";
    return;
  }
  const calibrated = !!(calib.mapping && calib.key === state.activeKey);
  let line = calibrated
    ? `本图 ${m.points.length} 个点位；缩放 < ${MARK_MIN_SCALE} 时不画（跟瓦片一样）`
    : '<b class="t-warn">这张图还没标定</b>：标记是世界坐标，没有标定就换不成像素';
  if (state.markHidden.size) {
    line += `<br>已隐藏 <b>${state.markHidden.size}</b> 个图标` +
            '<button id="hintClearFilter">全部显示</button>';
  }
  el.innerHTML = line;
  const clr = $("hintClearFilter");
  if (clr) clr.onclick = () => setAllMarkItems(true);
}

// ---------------- 点地图上的徽章 → 看它是谁、在哪 ----------------
// 徽章是**画在画布上的**（不是 DOM），所以得自己做命中测试：把点击位置换算成
// holder 坐标，再看落不落在某个徽章的圆里（圆心在锚点上方 ph + r 处）。
function hitTestMarker(clientX, clientY) {
  if (!state.layers.markers) return null;
  if (!showMarks()) return null;
  const s = mapView.scale;
  if (s < MARK_MIN_SCALE) return null;     // 这个缩放下根本没画，别误判
  const inv = (calib.mapping && calib.key === state.activeKey)
    ? calib.mapping.inverse_matrix : null;
  if (!inv) return null;
  const matrix = calib.mapping.matrix;
  const rect = canvasWrap.getBoundingClientRect();
  const hx = (clientX - rect.left - mapView.panX) / s;
  const hy = (clientY - rect.top - mapView.panY) / s;
  const r = (MARK_BADGE_PX / s) / 2;
  const ph = MARK_POINTER_PX / s;
  let best = null, bestD = Infinity;
  const vis = visibleRect();
  for (const [tid, wx, , wz] of markerCandidates(vis, matrix)) {
    const info = state.markers.templates[tid];
    if (!info || state.markHidden.has(tid)) continue;
    const px = inv[0][0] * wx + inv[0][1] * wz + inv[0][2];
    const py = inv[1][0] * wx + inv[1][1] * wz + inv[1][2];
    const d = Math.hypot(hx - px, hy - (py - ph - r));
    if (d <= r && d < bestD) { bestD = d; best = { tid, wx, wz, px, py }; }
  }
  return best;
}

function openLayerEntranceFromEvent(e) {
  if (e.button !== 0) return false;
  const hit = hitTestMarker(e.clientX, e.clientY);
  if (!hit || hit.tid !== LAYER_ENTRANCE_TID) return false;
  showMarkTip(null);
  void showLayerEntrancePicker(hit);
  e.preventDefault();
  return true;
}

function showMarkTip(hit) {
  const el = $("markTip");
  el.classList.remove("interactive");
  if (!hit) { el.style.display = "none"; return; }
  const info = state.markers.templates[hit.tid];
  const s = mapView.scale;
  const rows = [`<b>${info.n}</b>`];
  const cat = [info.m, info.s].filter(Boolean).filter((x, i, a) => a.indexOf(x) === i);
  if (cat.length) rows.push(cat.join(" · "));
  rows.push(`世界坐标 <span class="mono">${hit.wx.toFixed(1)}, ${hit.wz.toFixed(1)}</span>`);
  // 这张图有 2D 网格就顺带给出格子下标（网格编辑时对得上）
  const v = state.views.get(state.activeKey);
  const e = v && v.edit;
  if (e && e.cellSize && e.origin) {
    rows.push("<span class=\"mono\">格子 " +
      Math.floor((hit.wx - e.origin[0]) / e.cellSize) + ", " +
      Math.floor((hit.wz - e.origin[1]) / e.cellSize) + "</span>");
  }
  rows.push(`本图共 <span class="mono">${state.markers.points.filter(p => p[0] === hit.tid).length}</span> 个`);
  el.innerHTML = rows.join("<br>");
  el.style.left = (hit.px * s + mapView.panX) + "px";
  el.style.top = ((hit.py - MARK_POINTER_PX / s) * s + mapView.panY - 8) + "px";
  el.style.display = "block";
}

async function showLayerEntrancePicker(hit) {
  const key = state.activeKey;
  if (!key || !hit) return;
  await loadTierCatalog();
  if (state.activeKey !== key) return;

  const picked = pickLayerGroup(currentTierCatalog(), hit.wx, hit.wz);
  if (!picked) {
    showMarkTip(hit);
    return;
  }

  const el = $("markTip");
  const s = mapView.scale;
  const activeId = state.activeTier && state.activeTier.key === key
    ? state.activeTier.id : "";
  const options = picked.layers.map(layer =>
    '<button type="button" class="mark-layer-option' +
    (layer.id === activeId ? " active" : "") +
    '" data-tier="' + layer.id + '">' + layer.name + "</button>"
  ).join("");
  el.innerHTML =
    '<div class="mark-layer-title">' + picked.group + " · 分层入口</div>" +
    '<div class="mark-layer-list">' + options + "</div>" +
    '<button type="button" class="mark-layer-option quiet" data-tier="">退出分层</button>';
  el.classList.add("interactive");
  el.style.left = (hit.px * s + mapView.panX) + "px";
  el.style.top = ((hit.py - MARK_POINTER_PX / s) * s + mapView.panY - 8) + "px";
  el.style.display = "block";

  el.querySelectorAll("[data-tier]").forEach(button => {
    button.onclick = async () => {
      await setActiveTier(button.dataset.tier);
      await showLayerEntrancePicker(hit);
    };
  });
}

function setBusyFetch(b) {
  state.busyFetch = b;
  syncButtons();
}

// ---------------- 合成流程 ----------------
async function startCompose() {
  const map = $("selMap").value, zoom = $("selZoom").value;
  if (!map || !zoom) return;
  state.counts = { new: 0, updated: 0, unchanged: 0, skipped: 0, error: 0 };
  updateCounts();
  clearView(map, zoom);
  state.compose = { map, zoom, total: 0, done: 0 };
  syncButtons();
  $("progressLine").textContent = "合成 " + map + "/" + zoom + "…";
  try {
    const resp = await fetch("/api/compose", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ map, zoom, save: true }),
    });
    const res = await resp.json();
    if (!res.ok) { addLog("合成启动失败: " + res.error, "t-err"); state.compose = null; syncButtons(); return; }
    state.compose.total = res.total;
    addLog("开始合成 " + map + "/" + zoom + "，共 " + res.total + " 张瓦片", "t-ok");
  } catch (e) {
    // 请求本身失败时不会有 compose_done，不清掉 state.compose 的话
    // 「模拟抓取」会被永久禁用
    state.compose = null;
    syncButtons();
    addLog("合成请求失败: " + e.message, "t-err");
  }
}

// ---------------- SSE ----------------
function connect() {
  const es = new EventSource("/api/events");
  es.onopen = () => {
    setStatus("已连接服务器", "online");
    $("sideLinkState").textContent = "ONLINE";
  };
  es.onerror = () => {
    setStatus("连接断开，重连中…", "offline");
    $("sideLinkState").textContent = "RETRY";
  };
  es.onmessage = (ev) => {
    let evt;
    try { evt = JSON.parse(ev.data); } catch (e) { return; }
    handleEvent(evt);
  };
}

async function handleEvent(evt) {
  switch (evt.type) {
    case "hello":
      state.service = evt.service;
      $("servicePanel").innerHTML =
        "瓦片目录: <b>" + evt.service.tiles_root + "</b><br>" +
        "登录配置: <b>" + evt.service.profile_dir + "</b><br>" +
        "Playwright: " + (evt.service.playwright ? "✓" : "✗ 未安装") +
        "　Pillow: " + (evt.service.pillow ? "✓" : "✗ 未安装") +
        (evt.service.fetch.running ? "<br>抓取运行中: " + evt.service.fetch.session : "");
      if (evt.service.fetch.running) setBusyFetch(true);
      // 刷新页面时把服务端的实时计数带回来：否则抓取还在跑，而「新增/更新/
      // 未变化」已经静默归零，和标题上的「抓取运行中」自相矛盾
      if (evt.service.fetch && evt.service.fetch.counts) {
        state.counts = Object.assign(
          { new: 0, updated: 0, unchanged: 0, skipped: 0, error: 0 },
          evt.service.fetch.counts);
        updateCounts();
      }
      if (evt.service.position) {
        state.position = evt.service.position;
        renderPositionPanel();
      }
      break;
    case "tile": {
      if (state.follow) setActive(viewKey(evt.map, evt.zoom));
      const k = evt.kind;
      if (k === "new") state.counts.new++;
      else if (k === "updated") state.counts.updated++;
      else if (k === "unchanged") state.counts.unchanged++;
      else if (k === "sim") state.counts.new++;
      updateCounts();
      if (k === "existing" && state.compose && state.compose.map === evt.map
          && state.compose.zoom === evt.zoom) {
        state.compose.done = evt.seq || state.compose.done + 1;
        $("progressLine").textContent = "合成中: " + state.compose.done + " / " + state.compose.total;
      }
      // 只记账，不下载：像素等它进入可见区域再按需取
      noteTile(evt.map, evt.zoom, evt.x, evt.y, k);
      // 这张瓦片服务端现在有了：如果之前 404 过，作废那个"缺失"标记，
      // 让下一帧重新去取（这是替代定时重试的精确做法）
      forgetMissingTile(evt.map, evt.zoom, evt.x, evt.y);
      break;
    }
    case "compose_start":
      setActive(viewKey(evt.map, evt.zoom));
      updateCurBar();
      $("progressLine").textContent = "合成 " + evt.map + "/" + evt.zoom + ": 0 / " + evt.total;
      break;
    case "compose_bounds":
      if (state.activeKey === viewKey(evt.map, evt.zoom)) {
        const fresh = !hasBounds(state.views.get(state.activeKey));
        getView(evt.map, evt.zoom, { minX: evt.minX, maxX: evt.maxX, minY: evt.minY, maxY: evt.maxY });
        redrawActive();
        if (fresh) fitView();
        updateCurBar();
      }
      break;
    case "compose_done":
      $("progressLine").textContent = evt.file
        ? "合成完成: " + evt.total + " 张（已保存 tiles/maps/" + evt.file + "）"
        : "合成完成: " + evt.total + " 张";
      if (state.compose) state.compose = null;
      syncButtons();  // 只收合成的忙状态，别动正在跑的抓取
      addLog(evt.file
        ? "合成完成，已保存 tiles/maps/" + evt.file + "（覆盖旧版）"
        : "合成完成（未保存文件）", "t-ok");
      break;
    case "fetch_start":
      setBusyFetch(true);
      state.counts = { new: 0, updated: 0, unchanged: 0, skipped: 0, error: 0 };
      updateCounts();
      addLog("抓取会话: " + evt.session, "t-ok");
      break;
    case "fetch_done":
      setBusyFetch(false);
      // 服务端的 counts 才是权威：前端只在 tile 事件里逐张累加 new/updated/
      // unchanged，跳过和错误两个数永远是 0，用户看不到有瓦片被跳过或报错
      if (evt.counts) {
        state.counts = Object.assign(
          { new: 0, updated: 0, unchanged: 0, skipped: 0, error: 0 },
          evt.counts);
        updateCounts();
      }
      addLog("抓取结束: " + evt.session + "，合计 " + evt.total + " 张", "t-ok");
      // 会话目录里刚落盘的瓦片是可直接取的（它是 _compose_candidates 的第一候选），
      // 之前 404 过的格子现在可能有了
      forgetAllMissing();
      break;
    case "merged":
      // 合并到 tiles/latest 完成后整批瓦片都可能变了：一次性作废全部"缺失"标记。
      // 这是"404 不永久缓存"在批量场景下的那一半（单个场景由 tile 事件负责）。
      forgetAllMissing();
      break;
    case "simulate_done":
      addLog("模拟抓取完成: " + evt.total + " 张", "t-ok");
      break;
    case "marks_start":
      state.marks = Object.assign({}, state.marks || {},
        { running: true, stats: null, error: null });
      renderMarksPanel();
      break;
    case "marks_done":
      // 事件里只有 stats/error，凭证状态等字段要回 /api/marks/status 拿
      if (evt.error) addLog("标记抓取失败: " + evt.error, "t-err");
      await loadMarksStatus();
      break;
    case "log":
      addLog(evt.text, evt.text.indexOf("失败") >= 0 || evt.text.indexOf("异常") >= 0 ? "t-err" : "");
      break;
    case "coords":
      $("coordsReadout").textContent = JSON.stringify((evt.data || {}).pos || evt.data);
      {
        const d = evt.data || {};
        const pos = d.pos || d;
        if (pos && pos.x != null && pos.z != null) {
          updatePlayerMarker({ x: pos.x, y: pos.y, z: pos.z });
        }
        void syncPositionLocation(d);
      }
      break;
    case "position_state":
      state.position = evt.state;
      renderPositionPanel();
      break;
  }
}

// ---------------- UI 绑定 ----------------
function currentTierCatalog() {
  return state.tierCatalogs.get(state.activeKey) || [];
}

function currentTierId() {
  return state.activeTier && state.activeTier.key === state.activeKey
    ? state.activeTier.id : "";
}

function syncTierControls() {
  const sel = $("selTier");
  if (!sel) return;
  const layers = currentTierCatalog();
  const calibrated = !!(calib.mapping && calib.key === state.activeKey);
  sel.disabled = state.tierLoading || !layers.length || !calibrated;
  if (!layers.length) {
    sel.title = "当前底图没有内置的官方分层目录";
  } else if (!calibrated) {
    sel.title = "需先标定当前地图，才能把分层图片和底图对齐";
  } else {
    sel.title = "选择官方分层图片";
  }
}

function populateTierSelect() {
  const sel = $("selTier");
  if (!sel) return;
  const layers = currentTierCatalog();
  const activeId = state.activeTier && state.activeTier.key === state.activeKey
    ? state.activeTier.id : "";
  sel.innerHTML = "";
  const none = document.createElement("option");
  none.value = "";
  none.textContent = "关闭";
  sel.appendChild(none);
  const appendGroup = (label, items, textOf) => {
    if (!items.length) return;
    const group = document.createElement("optgroup");
    group.label = label;
    for (const layer of items) {
      const option = document.createElement("option");
      option.value = layer.id;
      option.textContent = textOf(layer);
      option.title = [
        layer.map_display_name,
        layer.level_name,
        layer.group,
        layer.name,
      ].filter(Boolean).join(" / ");
      group.appendChild(option);
    }
    sel.appendChild(group);
  };
  appendGroup(
    "分层地图",
    layers.filter(layer => layer.map_category !== "independent"),
    layer => layer.level_name + " · " + layer.name,
  );
  appendGroup(
    "独立地图",
    layers.filter(layer => layer.map_category === "independent"),
    layer => layer.group + " · " + layer.name,
  );
  sel.value = activeId;
  syncTierControls();
}

function closeTierBitmap(tier) {
  if (!tier) return;
  for (const bitmap of [tier.bitmap, tier.preview]) {
    if (bitmap && typeof bitmap.close === "function") bitmap.close();
  }
}

async function decodeTierBitmap(blob, meta, maxEdge = 0) {
  if (!window.createImageBitmap) return await loadImageFromBlob(blob);
  const width = Number(meta.width) || 0;
  const height = Number(meta.height) || 0;
  const longest = Math.max(width, height);
  if (!maxEdge || !longest || longest <= maxEdge) {
    return await createImageBitmap(blob);
  }
  const scale = maxEdge / longest;
  return await createImageBitmap(blob, {
    resizeWidth: Math.max(1, Math.round(width * scale)),
    resizeHeight: Math.max(1, Math.round(height * scale)),
    resizeQuality: "high",
  });
}

function syncTierResolution() {
  const tier = state.activeTier;
  if (!tier || tier.key !== state.activeKey || !tier.blob) return;
  if (mapView.scale >= TIER_FULL_MIN_SCALE &&
      !tier.bitmap && !tier.fullLoading) {
    tier.fullLoading = true;
    const requestId = state.tierRequestId;
    decodeTierBitmap(tier.blob, tier.meta).then(bitmap => {
      if (requestId !== state.tierRequestId || state.activeTier !== tier) {
        if (bitmap && typeof bitmap.close === "function") bitmap.close();
        return;
      }
      if (mapView.scale < TIER_FULL_RELEASE_SCALE) {
        if (bitmap && typeof bitmap.close === "function") bitmap.close();
        tier.fullLoading = false;
        return;
      }
      tier.bitmap = bitmap;
      if (tier.preview && typeof tier.preview.close === "function") {
        tier.preview.close();
      }
      tier.preview = null;
      tier.previewLoading = false;
      tier.fullLoading = false;
      requestRender(RENDER_LAYERS.BASE);
    }).catch(e => {
      tier.fullLoading = false;
      addLog("分层高清图加载失败: " + e.message, "t-warn");
    });
  } else if (mapView.scale < TIER_FULL_RELEASE_SCALE && tier.bitmap) {
    if (typeof tier.bitmap.close === "function") tier.bitmap.close();
    tier.bitmap = null;
    tier.fullLoading = false;
    requestRender(RENDER_LAYERS.BASE);
  }
  if (mapView.scale < TIER_FULL_MIN_SCALE &&
      !tier.preview && !tier.previewLoading) {
    tier.previewLoading = true;
    const requestId = state.tierRequestId;
    decodeTierBitmap(tier.blob, tier.meta, TIER_PREVIEW_MAX).then(preview => {
      if (requestId !== state.tierRequestId || state.activeTier !== tier) {
        if (preview && typeof preview.close === "function") preview.close();
        return;
      }
      tier.preview = preview;
      tier.previewLoading = false;
      requestRender(RENDER_LAYERS.BASE);
    }).catch(() => {
      tier.previewLoading = false;
    });
  }
}

function clearActiveTier(render = true, key = state.activeKey) {
  state.tierRequestId++;
  state.tierLoading = false;
  if (state.activeTier) closeTierBitmap(state.activeTier);
  state.activeTier = null;
  const v = state.views.get(key);
  if (v) v.tierId = null;
  if (render && state.activeKey) requestRender(RENDER_LAYERS.BASE);
  populateTierSelect();
}

async function loadTierCatalog() {
  const key = state.activeKey;
  if (!key) return;
  if (state.tierCatalogs.has(key)) {
    populateTierSelect();
    return;
  }
  const [map, zoom] = key.split("@");
  try {
    const resp = await fetch("/api/layers?map=" + encodeURIComponent(map) +
                             "&zoom=" + encodeURIComponent(zoom));
    const res = await resp.json();
    if (!resp.ok || res.ok === false) {
      throw new Error(res.error || ("HTTP " + resp.status));
    }
    state.tierCatalogs.set(key, Array.isArray(res.layers) ? res.layers : []);
  } catch (e) {
    addLog("加载分层目录失败: " + e.message, "t-err");
    state.tierCatalogs.set(key, []);
  }
  if (state.activeKey === key) populateTierSelect();
  renderLinkList();
}

async function loadFloorRegionMap(force = false) {
  const key = state.activeKey;
  if (!key) return;
  await loadTierCatalog();
  const activeId = currentTierId();
  const layer = currentTierCatalog().find(item => item.id === activeId);
  const levelId = String(layer && layer.level_id || "");
  const mapKey = key + "|" + (levelId || "__all__");
  if (!force && state.floorMap.key === mapKey) return;
  state.floorHighlight = { componentId: "", layerId: "" };
  state.floorPeerGrids = new Map();
  const [map, zoom] = key.split("@");
  try {
    const res = await fetch(
      "/api/grid2d/floor-map?map=" + encodeURIComponent(map) +
      "&zoom=" + encodeURIComponent(zoom) +
      "&level_id=" + encodeURIComponent(levelId),
    ).then(response => response.json());
    if (!res.ok) throw new Error(res.error || "读取楼层范围失败");
    if (state.activeKey !== key || currentTierId() !== activeId) return;
    state.floorMap = {
      key: mapKey,
      levelId,
      layers: res.layers || [],
      error: null,
    };
    await loadFloorOverlaps(true);
  } catch (e) {
    state.floorMap = {
      key: mapKey, levelId, layers: [], error: e.message,
    };
    addLog("加载楼层重合范围失败: " + e.message, "t-warn");
  }
  requestRender(RENDER_LAYERS.SELECTION);
}

async function loadFloorOverlaps(force = false) {
  const key = state.activeKey;
  const tierId = currentTierId() || "__main__";
  const levelId = state.floorMap.levelId;
  if (!key) {
    state.floorOverlap = {
      key: "", xzOnly: new Set(), threeD: new Set(),
      resolved: new Set(), components: [],
    };
    requestRender(RENDER_LAYERS.SELECTION);
    return;
  }
  const mapKey = key + "|" + tierId + "|" + levelId;
  if (!force && state.floorOverlap.key === mapKey) return;
  const [map, zoom] = key.split("@");
  try {
    const res = await fetch(
      "/api/grid2d/overlaps?map=" + encodeURIComponent(map) +
      "&zoom=" + encodeURIComponent(zoom) +
      "&tier=" + encodeURIComponent(tierId) +
      "&level_id=" + encodeURIComponent(levelId),
    ).then(response => response.json());
    if (!res.ok) throw new Error(res.error || "计算网格重合失败");
    if (state.activeKey !== key ||
        (currentTierId() || "__main__") !== tierId) return;
    const components = (res.components || []).map(component => {
      const resolved = component.layers.every(targetTier => {
        const layer = (state.floorMap.layers || [])
          .find(item => item.tier_id === targetTier);
        return !!floorRegionForComponent(layer, component);
      });
      return { ...component, resolved };
    });
    const resolvedCells = new Set();
    const xzOnly = new Set();
    const threeD = new Set();
    for (const component of components) {
      const target = component.resolved
        ? resolvedCells
        : component.severity === "three_d" ? threeD : xzOnly;
      for (const cell of component.cells || []) {
        target.add(cell[0] + "," + cell[1]);
      }
    }
    state.floorOverlap = {
      key: mapKey,
      xzOnly,
      threeD,
      resolved: resolvedCells,
      components,
    };
    populateFloorComponentSelect();
    renderFloorMarkedList();
    void loadFloorPeerGrids(true);
  } catch (e) {
    state.floorOverlap = {
      key: mapKey, xzOnly: new Set(), threeD: new Set(),
      resolved: new Set(), components: [],
    };
    addLog("计算网格重合失败: " + e.message, "t-warn");
    populateFloorComponentSelect();
    renderFloorMarkedList();
  }
  requestRender(RENDER_LAYERS.SELECTION);
}

async function setActiveTier(layerId) {
  const key = state.activeKey;
  if (!key) return;
  const oldTierId = currentTierId();
  if ((edit.mode || link.mode) && edit.dirty && oldTierId !== layerId) {
    setStatus("切换分层前请先保存当前层网格", "warning");
    populateTierSelect();
    return;
  }
  if (!layerId) {
    clearActiveTier();
    if (edit.mode || link.mode) void loadGrid2D(true);
    await loadFloorRegionMap(true);
    requestRender(RENDER_LAYERS.LINK);
    setStatus("已关闭分层显示", "ready");
    return;
  }
  if (!calib.mapping || calib.key !== key) {
    addLog("分层图片需要先完成当前地图标定", "t-warn");
    populateTierSelect();
    return;
  }
  const layer = currentTierCatalog().find(item => item.id === layerId);
  if (!layer) {
    addLog("找不到分层配置: " + layerId, "t-err");
    populateTierSelect();
    return;
  }

  const requestId = ++state.tierRequestId;
  state.tierLoading = true;
  syncTierControls();
  setStatus("正在加载分层: " + layer.name + "…", "active");
  const [map, zoom] = key.split("@");
  try {
    const resp = await fetch("/api/layer-image?map=" + encodeURIComponent(map) +
                             "&zoom=" + encodeURIComponent(zoom) +
                             "&tier=" + encodeURIComponent(layerId));
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const blob = await resp.blob();
    const preview = await decodeTierBitmap(blob, layer, TIER_PREVIEW_MAX);
    if (requestId !== state.tierRequestId || state.activeKey !== key) {
      if (preview && typeof preview.close === "function") preview.close();
      return;
    }
    if (state.activeTier) closeTierBitmap(state.activeTier);
    state.activeTier = {
      key, id: layerId, meta: layer, blob, preview,
      bitmap: null, fullLoading: false, previewLoading: false,
    };
    state.tierLoading = false;
    const v = state.views.get(key);
    if (v) v.tierId = layerId;
    populateTierSelect();
    syncTierResolution();
    requestRender(RENDER_LAYERS.BASE);
    if ((edit.mode || link.mode) && oldTierId !== layerId) {
      void loadGrid2D(true);
    }
    await loadFloorRegionMap(true);
    requestRender(RENDER_LAYERS.LINK);
    setStatus("已显示分层: " + layer.group + " / " + layer.name, "ready");
    addLog("已加载官方分层图片: " + layer.filename, "t-ok");
  } catch (e) {
    if (requestId !== state.tierRequestId || state.activeKey !== key) return;
    state.tierLoading = false;
    populateTierSelect();
    setStatus("分层图片加载失败", "warning");
    addLog("分层图片加载失败: " + e.message, "t-err");
  }
}

async function loadTileMaps() {
  try {
    const resp = await fetch("/api/tilemaps");
    const res = await resp.json();
    const maps = res.maps || {};
    state.tileMaps = maps;
    if (state.position && state.position.last_position) {
      void syncPositionLocation(state.position.last_position);
    }
    const selM = $("selMap"), selZ = $("selZoom");
    selM.innerHTML = "";
    for (const m of Object.keys(maps).sort()) {
      const o = document.createElement("option");
      o.value = m;
      o.textContent = mapDisplayName(m);
      o.title = m;
      selM.appendChild(o);
    }
    const fillZooms = () => {
      const zooms = (maps[selM.value] || []).slice().sort((a, b) => +a - +b);
      selZ.innerHTML = "";
      for (const z of zooms) {
        const o = document.createElement("option");
        o.value = z; o.textContent = z;
        selZ.appendChild(o);
      }
      if (zooms.length) selZ.value = zooms[zooms.length - 1]; // 默认最高 zoom
    };
    // 真正“选择”：切换地图/zoom 后立即在画布上显示该地图
    selM.onchange = () => { fillZooms(); openSelectedMap(); };
    selZ.onchange = openSelectedMap;
    fillZooms();
    if (!state.activeKey) void openSelectedMap();
  } catch (e) {
    addLog("加载地图列表失败: " + e.message, "t-err");
  }
}

// 下拉选择后：内存视图 → 已存总图 → 自动流式合成，依次尝试显示
async function openSelectedMap({ preserveFollow = false } = {}) {
  const map = $("selMap").value, zoom = $("selZoom").value;
  if (!map || !zoom) return;
  const displayName = mapDisplayName(map);
  if (!preserveFollow) {
    state.follow = false;             // 手动选择后固定视图，不再被抓取流切走
    $("chkFollow").checked = false;
  }
  const key = viewKey(map, zoom);

  // 1) 内存里已经有这张图的范围（之前合成/抓取过）→ 直接显示，瓦片按需取
  const v0 = state.views.get(key);
  if (hasBounds(v0) && v0.pasted.size > 0) {
    setActive(key);
    fitView();
    setStatus("已显示 " + displayName + " / L" + zoom + "（内存视图）", "ready");
    return;
  }
  setActive(key);

  // 2) 有已保存的总图 → 只取服务端生成的缩略图打底
  //    （以前是下 78.9MB 整图再解码成 1.7 亿像素的画布，这是打开就卡的主因）
  try {
    const resp = await fetch("/api/maps?map=" + encodeURIComponent(map) + "&zoom=" + encodeURIComponent(zoom));
    const info = await resp.json();
    if (info && info.file && info.minX !== undefined) {
      getView(map, zoom, { minX: info.minX, maxX: info.maxX, minY: info.minY, maxY: info.maxY });
      redrawActive();
      fitView();
      setStatus("已显示 " + displayName + " / L" + zoom +
                "（缩略总图，放大后自动加载瓦片）", "ready");
      addLog("下拉选择: 载入缩略总图 " + info.file, "t-ok");
      loadOverview(map, zoom);
      return;
    }
  } catch (e) {
    addLog("加载总图信息失败: " + e.message, "t-err");
  }

  // 3) 都没有 → 自动流式合成（瓦片逐张贴上，可实时观察）
  setStatus("该地图无已存总图，自动开始合成…", "active");
  startCompose();
}

$("btnCompose").onclick = startCompose;
$("btnMarksAuth").onclick = () => startMarks();
$("btnPosToggle").onclick = () => togglePositionStream();
$("chkWs").onchange = () => togglePositionStream();
$("btnFetch").onclick = async () => {
  try {
    const resp = await fetch("/api/fetch/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ headless: $("chkHeadless").checked }),
    });
    const res = await resp.json();
    if (!res.ok) addLog("抓取启动失败: " + res.error, "t-err");
  } catch (e) { addLog("抓取请求失败: " + e.message, "t-err"); }
};
$("btnFetchStop").onclick = async () => {
  await fetch("/api/fetch/stop", { method: "POST" });
  addLog("已请求停止抓取…");
};
$("btnSim").onclick = async () => {
  try {
    const resp = await fetch("/api/simulate/start", { method: "POST" });
    const res = await resp.json();
    if (!res.ok) addLog("模拟启动失败: " + res.error, "t-err");
  } catch (e) { addLog("模拟请求失败: " + e.message, "t-err"); }
};
$("btnReset").onclick = () => {
  if (state.activeKey) {
    const [map, zoom] = state.activeKey.split("@");
    clearView(map, zoom);
    addLog("已清空画布: " + state.activeKey);
  }
};
$("btnDownload").onclick = async () => {
  const [map, zoom] = (state.activeKey || "").split("@");
  if (!map || !zoom) { addLog("当前没有选中的地图", "t-err"); return; }
  try {
    // 画布现在只有一屏大小，不能拿它当总图导出；直接下服务端保存的拼接结果
    const resp = await fetch("/api/maps?map=" + encodeURIComponent(map) + "&zoom=" + encodeURIComponent(zoom));
    const info = await resp.json();
    if (!info || !info.file) { addLog("该地图还没有保存过的总图，请先点“合成总图”", "t-err"); return; }
    const a = document.createElement("a");
    a.href = "/maps/" + info.file;
    a.download = (state.activeKey || "map").replace("@", "_") + ".png";
    a.click();
    addLog("已下载总图 " + info.file);
  } catch (e) { addLog("下载失败: " + e.message, "t-err"); }
};
$("chkFollow").onchange = () => { state.follow = $("chkFollow").checked; };
$("mpAll").onclick = () => setAllMarkItems(true);
$("mpNone").onclick = () => setAllMarkItems(false);
$("mpReset").onclick = resetMarkFilter;
$("btnFit").onclick = fitView;
$("btnSideToggle").onclick = () => setSidePanel(!isSidePanelOpen());
$("btnUndo").onclick = () => {
  if (edit.mode) undoGridEdit();
  else if (calib.mode) undoCalibEdit();
};
$("btnRedo").onclick = () => {
  if (edit.mode) redoGridEdit();
  else if (calib.mode) redoCalibEdit();
};
document.querySelectorAll(".rail-mode").forEach(btn => {
  // 透视按钮只是"打开 3D 视图"，不属于模式单选组。
  if (!btn.dataset.railMode) return;
  btn.onclick = () => {
    const mode = btn.dataset.railMode;
    setMode(mode);
    setSideTab(DEFAULT_SIDE_TAB[currentMode()], { syncMode: false });
  };
});

// three.js 按需动态加载：只有真的打开过透视视图才付这份体积。
let perspectiveView = null;

function setPerspectiveButton(on) {
  const button = $("btnPerspective");
  button.classList.toggle("is-active", on);
  button.setAttribute("aria-pressed", String(on));
}

function closePerspective() {
  if (perspectiveView) perspectiveView.close();
  setPerspectiveButton(false);
}

async function openPerspective() {
  const key = state.activeKey;
  if (!key) {
    setStatus("先选择一张要透视查看的地图", "warning");
    return;
  }
  const [map, zoom] = key.split("@");
  setPerspectiveButton(true);
  try {
    if (!perspectiveView) {
      const module = await import("./three/perspective-view.js");
      perspectiveView = new module.PerspectiveView($("pvView"));
      $("pvClose").onclick = closePerspective;
    }
    await perspectiveView.show({ map, zoom, tier: currentTierId() });
  } catch (e) {
    addLog("透视视图加载失败: " + e.message, "t-err");
    setStatus("透视视图加载失败: " + e.message, "warning");
    closePerspective();
  }
}

$("btnPerspective").onclick = () => {
  if (perspectiveView && perspectiveView.open) closePerspective();
  else void openPerspective();
};
document.querySelectorAll("[data-side-tab]").forEach(btn => {
  btn.onclick = () => setSideTab(btn.dataset.sideTab);
});
$("sideBackdrop").onclick = () => setSidePanel(false);
$("calibOk").onclick = submitCalibPoint;
$("calibCancel").onclick = closeCalibDialog;
$("calibX").addEventListener("keydown", (e) => { if (e.key === "Enter") submitCalibPoint(); });
$("calibZ").addEventListener("keydown", (e) => { if (e.key === "Enter") submitCalibPoint(); });
// 网格模式的快捷键。只在 edit 模式下生效，且正在输入框里打字时不抢键
// （宽度输入框、控制点坐标框都要能正常输入数字）。
const BRUSH_BY_KEY = { "1": "free", "2": "blocked", "3": "erase" };

function isTextEditingTarget(target) {
  return !!target && (
    target.tagName === "INPUT" ||
    target.tagName === "TEXTAREA" ||
    target.tagName === "SELECT" ||
    target.isContentEditable
  );
}

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (perspectiveView && perspectiveView.open) closePerspective();
    else if (isSidePanelOpen()) setSidePanel(false);
    else if ($("calibDlg").style.display === "block") closeCalibDialog();
    else if ($("floorRegionDlg").style.display === "block") closeFloorRegionDialog();
    else if (edit.mode) clearSelection();
    return;
  }

  const mod = e.ctrlKey || e.metaKey;
  const key = e.key.toLowerCase();
  if (mod && !e.altKey && key === "s") {
    if (!edit.mode && !calib.mode) return;
    e.preventDefault();
    if (edit.mode) saveGrid2D();
    else if (!$("btnCalibSave").disabled) saveCalib();
    else setStatus("至少需要 3 个启用中的控制点才能保存标定", "warning");
    return;
  }
  if (mod && !e.altKey && (key === "z" || key === "y")) {
    if (isTextEditingTarget(e.target)) return;
    if (!edit.mode && !calib.mode) return;
    e.preventDefault();
    const redo = key === "y" || e.shiftKey;
    if (edit.mode) {
      if (redo) redoGridEdit();
      else undoGridEdit();
    } else if (redo) {
      redoCalibEdit();
    } else {
      undoCalibEdit();
    }
    return;
  }

  if (!edit.mode) return;
  const t = e.target;
  if (isTextEditingTarget(t)) return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const brush = BRUSH_BY_KEY[e.key];
  if (brush) {
    // 1/2/3 = "用这个画笔做事"：
    //   框选着格子 → 直接把选中的填成该状态（这是框完收手的收尾动作）
    //   没有框选   → 只是切画笔，并进入"按住"状态（滚轮可以调粗细、HUD 出现）
    // 填完就不再进按住状态：动作已经做完了，再让滚轮去改粗细没有意义。
    if (hasSelection()) {
      setBrush(brush);
      if (!e.repeat) applySelection(brush);
    } else {
      heldBrushKeys.add(e.key);
      if (!e.repeat) { setBrush(brush); showBrushHud(); }
    }
    e.preventDefault();
    return;
  }
});

document.addEventListener("keyup", (e) => {
  if (!heldBrushKeys.delete(e.key)) return;
  if (!heldBrushKeys.size) hideBrushHud();   // 全松开了才收 HUD
});

// 切标签/切窗口时 keyup 收不到，按键会卡在"按住"状态
window.addEventListener("blur", () => { heldBrushKeys.clear(); hideBrushHud(); });
$("btnCalibSave").onclick = saveCalib;
$("btnCalibClear").onclick = () => {
  if (!calib.points.length && !calib.mapping) return;
  pushCalibHistory();
  calib.points = []; calib.mapping = null;
  $("calibStatus").textContent = "未标定（开标定模式后点击地图添加控制点）";
  renderCalibList(); renderCalibMarks();
};
// ---------------- 画笔：按钮 / 快捷键 / 按住滚轮调粗细 ----------------
const heldBrushKeys = new Set();   // 当前按住的画笔键（1/2/3）

function brushLabel(name) {
  return name === "free" ? "Free" : name === "blocked" ? "Blocked" : "未知";
}

// 画笔 = 左键涂什么，也决定"填充选中"填什么（快捷键 1/2/3 走同一个入口）
function setBrush(name) {
  edit.brush = name;
  document.querySelectorAll(".brushBtn").forEach(x =>
    x.classList.toggle("on", x.dataset.brush === name));
  if (heldBrushKeys.size) showBrushHud();   // 按住时改画笔，HUD 跟着换
  if (edit.cursorCell) updateBrushCursor(edit.cursorCell[0], edit.cursorCell[1]);
}

// 粗细只有一个来源（edit.brushSize），输入框只是它的一个视图
function setBrushSize(n) {
  edit.brushSize = Math.max(1, Math.min(50, parseInt(n, 10) || 1));
  $("editBrush").value = edit.brushSize;
  if (heldBrushKeys.size) showBrushHud();
  if (edit.cursorCell) updateBrushCursor(edit.cursorCell[0], edit.cursorCell[1]);
}

// 按住画笔键时贴着光标显示"当前画笔 + 粗细"。和 markTip 一样是屏幕空间定位，
// 所以挂在 canvasWrap 下、用相对它的 left/top，不跟地图缩放走。
function showBrushHud() {
  const el = $("brushHud"), p = edit.pointer;
  if (!el || !p) return;
  el.dataset.brush = edit.brush;
  el.innerHTML = '<span class="bh-brush">' + brushLabel(edit.brush) +
                 '</span> · 粗细 <span class="bh-n">' + edit.brushSize + "</span>";
  el.style.display = "block";
  // canvasWrap 是 overflow:hidden，贴着右/下边时 HUD 会被裁掉——先量一下尺寸，
  // 放不下就翻到光标另一侧，再夹进可视区
  const gap = 16, pad = 4;
  const w = el.offsetWidth, h = el.offsetHeight;
  const cw = canvasWrap.clientWidth, ch = canvasWrap.clientHeight;
  let x = p.x + gap, y = p.y + gap;
  if (x + w + pad > cw) x = p.x - gap - w;
  if (y + h + pad > ch) y = p.y - gap - h;
  el.style.left = Math.max(pad, x) + "px";
  el.style.top = Math.max(pad, y) + "px";
}
function hideBrushHud() { const el = $("brushHud"); if (el) el.style.display = "none"; }

function hideBrushCursor() {
  brushCursor.style.display = "none";
  edit.cursorCell = null;
}

function updateBrushCursor(ix, iz) {
  edit.cursorCell = [ix, iz];
  if (!edit.mode || !state.layers.grid || !calib.mapping) {
    hideBrushCursor();
    return;
  }
  const width = edit.brushSize;
  const half = Math.floor((width - 1) / 2);
  const startX = ix - half;
  const startZ = iz - half;
  const rect = cellRangeRectPx(startX, startZ, startX + width, startZ + width);
  let left = rect.x * mapView.scale + mapView.panX;
  let top = rect.y * mapView.scale + mapView.panY;
  const minCursor = brushCursorMinPx;
  const rawW = rect.w * mapView.scale;
  const rawH = rect.h * mapView.scale;
  let screenW = Math.max(minCursor, rawW);
  let screenH = Math.max(minCursor, rawH);
  if (screenW === minCursor) left -= (screenW - rawW) / 2;
  if (screenH === minCursor) top -= (screenH - rawH) / 2;
  brushCursor.dataset.brush = edit.brush;
  brushCursorLabel.textContent = width > 1 ? String(width) : "";
  brushCursor.style.left = left + "px";
  brushCursor.style.top = top + "px";
  brushCursor.style.width = screenW + "px";
  brushCursor.style.height = screenH + "px";
  brushCursor.style.display = "block";
}

document.querySelectorAll(".brushBtn").forEach(b => {
  b.onclick = () => setBrush(b.dataset.brush);
});
$("btnLinkSource").onclick = () => setLinkStage("source");
$("btnLinkTarget").onclick = () => setLinkStage("target");
$("btnLinkSave").onclick = () => void saveGridLink();
$("btnLinkClear").onclick = clearLinkDraft;
$("btnEditLoad").onclick = () => loadGrid2D(true);
$("btnEditSave").onclick = saveGrid2D;
$("btnSelFree").onclick = () => applySelection("free");
$("btnSelBlocked").onclick = () => applySelection("blocked");
$("btnSelErase").onclick = () => applySelection("erase");
$("btnSelClear").onclick = clearSelection;
$("btnFloorRegion").onclick = openFloorRegionDialog;
$("floorComponent").onchange = () => void onFloorTargetChange();
$("floorLayer").onchange = () => void onFloorLayerChange();
$("floorSave").onclick = () => void saveFloorRegion();
$("floorClear").onclick = () => void clearFloorRegions();
$("floorCancel").onclick = closeFloorRegionDialog;
$("floorMarkedList").onclick = event => {
  const focusBtn = event.target.closest("[data-floor-focus]");
  const removeBtn = event.target.closest("[data-floor-remove]");
  if (focusBtn) {
    const row = state.floorMarkedRows[Number(focusBtn.dataset.floorFocus)];
    if (row) focusFloorRegion(row.region);
  } else if (removeBtn) {
    const row = state.floorMarkedRows[Number(removeBtn.dataset.floorRemove)];
    if (row) void removeFloorRegion(row.layer, row.region);
  }
};
$("btnPathMark").onclick = markPath;
$("btnPathClear").onclick = clearPath;
// 注意用 isNaN 判定而非 `|| 默认值`：后者会把用户输入的 0 当成"空"而忽略掉
$("editCellSize").onchange = () => {
  const e = ed(), n = parseFloat($("editCellSize").value);
  if (e && n > 0) {
    beginEditTransaction(e, true);
    e.cellSize = n;
    commitEditTransaction(e);
    setGridDirty(true);
    updateEditStats();
    requestRender(RENDER_LAYERS.GRID | RENDER_LAYERS.SELECTION);
    scheduleFloorOverlapLiveRefresh();
  }
};
$("editOriginX").onchange = () => {
  const e = ed(), n = parseFloat($("editOriginX").value);
  if (e && !isNaN(n)) {
    beginEditTransaction(e, true);
    e.origin[0] = n;
    commitEditTransaction(e);
    setGridDirty(true);
    updateEditStats();
    requestRender(RENDER_LAYERS.GRID | RENDER_LAYERS.SELECTION);
    scheduleFloorOverlapLiveRefresh();
  }
};
$("editOriginZ").onchange = () => {
  const e = ed(), n = parseFloat($("editOriginZ").value);
  if (e && !isNaN(n)) {
    beginEditTransaction(e, true);
    e.origin[1] = n;
    commitEditTransaction(e);
    setGridDirty(true);
    updateEditStats();
    requestRender(RENDER_LAYERS.GRID | RENDER_LAYERS.SELECTION);
    scheduleFloorOverlapLiveRefresh();
  }
};
$("editBrush").onchange = () => setBrushSize($("editBrush").value);

const layerControls = {
  layerBase: "base",
  layerTier: "tier",
  layerMarkers: "markers",
  layerGrid: "grid",
  layerSelection: "selection",
  layerLinks: "links",
  layerPath: "path",
};

Object.entries(layerControls).forEach(([id, layer]) => {
  $(id).checked = state.layers[layer];
  $(id).onchange = () => {
    state.layers[layer] = $(id).checked;
    if (layer === "base" || layer === "tier" || layer === "markers") {
      requestRender(RENDER_LAYERS.BASE);
    }
    else if (layer === "grid") {
      if ($(id).checked) invalidateGridLayer();
      requestRender(RENDER_LAYERS.GRID);
    }
    else if (layer === "selection") requestRender(RENDER_LAYERS.SELECTION);
    else if (layer === "links") requestRender(RENDER_LAYERS.LINK);
    else requestRender(RENDER_LAYERS.PATH);
  };
});

$("selTier").onchange = () => void setActiveTier($("selTier").value);
syncTierControls();
updateLinkEditorUi();

$("gridOpacity").value = String(state.gridOpacity);
editCanvasEl.style.opacity = "";
$("gridOpacity").oninput = () => {
  state.gridOpacity = Number($("gridOpacity").value);
  invalidateGridLayer();
  requestRender(RENDER_LAYERS.GRID);
};

// 画布浮层中的交互控件不能把 pointerdown 冒泡给 canvasWrap，否则点击
// 图层开关或底部工具栏会同时开始涂色、框选或平移。
["canvasToolbar", "layerPanel", "markTip"].forEach(id => {
  const overlay = $(id);
  ["pointerdown", "pointermove", "pointerup", "pointercancel", "wheel",
   "dblclick", "contextmenu"].forEach(type => {
    overlay.addEventListener(type, event => event.stopPropagation());
  });
});

// ---------------- 总图拖动 / 缩放 / 涂色 交互 ----------------
canvasWrap.addEventListener("pointerdown", (e) => {
  gestureRect = canvasWrap.getBoundingClientRect();
  // 网格/连接模式下，入口徽章优先于涂格和连线框选：点它只负责切层。
  if ((edit.mode || link.mode) && openLayerEntranceFromEvent(e)) return;
  if (link.mode) {
    if (e.button === 0) {
      const p = canvasPixelOfEvent(e);
      const c = p ? cellAtPixel(p[0], p[1]) : null;
      if (c) {
        link.drag = {
          id: e.pointerId,
          rect: { x0: c[0], z0: c[1], x1: c[0], z1: c[1] },
        };
        canvasWrap.setPointerCapture(e.pointerId);
        requestRender(RENDER_LAYERS.LINK);
        e.preventDefault();
      }
    } else if (e.button === 2) {
      startPanGesture(e);
      e.preventDefault();
    }
    return;
  }
  if (edit.mode) {
    if (e.button === 0) {
      if (e.shiftKey) {
        // Shift+左键：框选矩形
        canvasWrap.setPointerCapture(e.pointerId);
        const p = canvasPixelOfEvent(e);
        if (p) { const c = cellAtPixel(p[0], p[1]); if (c) startMarquee(c[0], c[1], e.pointerId); }
        e.preventDefault();
      } else {
        // 左键：涂格子（拖动连线）；开始新涂刷时清除旧选择
        clearSelection();
        const grid = ed();
        if (grid) beginEditTransaction(grid);
        edit.painting = { id: e.pointerId };
        edit.pendingPaint = { id: e.pointerId, clientX: e.clientX, clientY: e.clientY };
        canvasWrap.setPointerCapture(e.pointerId);
        edit.lastCell = null;
        queuePaintFrame();
        e.preventDefault();
      }
    } else if (e.button === 2) {
      // 右键：拖动平移
      startPanGesture(e, true);
      e.preventDefault();
    }
    return;
  }
  if (e.button !== 0 && e.pointerType === "mouse") return;
  showMarkTip(null);   // 开始平移就把徽章提示收起来，别拖着一块跑
  startPanGesture(e);
  e.preventDefault();
});
canvasWrap.addEventListener("pointermove", (e) => {
  if (link.mode && link.drag && e.pointerId === link.drag.id) {
    const p = canvasPixelOfEvent(e);
    const c = p ? cellAtPixel(p[0], p[1]) : null;
    if (c) {
      link.drag.rect.x1 = c[0];
      link.drag.rect.z1 = c[1];
      requestRender(RENDER_LAYERS.LINK);
    }
    e.preventDefault();
    return;
  }
  if (edit.mode) {
    const rect = gestureRect || canvasWrap.getBoundingClientRect();
    edit.pointer = { x: e.clientX - rect.left, y: e.clientY - rect.top };
    const point = canvasPixelOfClient(e.clientX, e.clientY, rect);
    if (point) {
      const cell = cellAtPixel(point[0], point[1]);
      if (cell) updateBrushCursor(cell[0], cell[1]);
      else hideBrushCursor();
    }
    if (heldBrushKeys.size) showBrushHud();
  }
  if (dragState && e.pointerId === dragState.id) {
    if (!dragState.moved &&
        (Math.abs(e.clientX - dragState.sx) + Math.abs(e.clientY - dragState.sy) > 4)) {
      dragState.moved = true;
    }
    if (dragState.moved) {
      activatePanBuffer();
      mapView.panX = dragState.px + (e.clientX - dragState.sx);
      mapView.panY = dragState.py + (e.clientY - dragState.sy);
      clampPan();
      if (!viewportInsideDragBuffer()) applyView(true);
      else {
        applyViewTransform();
        scheduleViewFrame(false);
      }
      e.preventDefault();
    }
    return;
  }
  if (edit.mode && edit.marquee && e.pointerId === edit.marquee.id) {
    const p = canvasPixelOfEvent(e);
    if (p) { const c = cellAtPixel(p[0], p[1]); if (c) updateMarquee(c[0], c[1]); }
    e.preventDefault();
    return;
  }
  if (edit.mode && edit.painting && e.pointerId === edit.painting.id) {
    edit.pendingPaint = { id: e.pointerId, clientX: e.clientX, clientY: e.clientY };
    queuePaintFrame();
    e.preventDefault();
    return;
  }
});
const endDrag = (e) => {
  if (link.mode && link.drag && e.pointerId === link.drag.id) {
    const r = link.drag.rect;
    const rect = {
      x0: Math.min(r.x0, r.x1), x1: Math.max(r.x0, r.x1),
      z0: Math.min(r.z0, r.z1), z1: Math.max(r.z0, r.z1),
    };
    const endpoint = endpointFromRect(rect);
    if (endpoint) {
      if (link.stage === "target") link.target = endpoint;
      else link.source = endpoint;
      link.stage = link.stage === "target" ? "source" : "target";
    }
    link.drag = null;
    updateLinkEditorUi();
    requestRender(RENDER_LAYERS.LINK);
    gestureRect = null;
    return;
  }
  if (dragState && e.pointerId === dragState.id) {
    const wasTap = !dragState.moved;
    dragState = null;
    canvasWrap.classList.remove("dragging");
    document.body.classList.remove("map-panning");
    finishPanGesture();
    // 真的拖动过 = 视线挪到了别处：缩放时记的焦点作废，退回视口中心
    if (!wasTap) mapView.focus = null;
    // 标定模式下：轻点地图 → 取控制点
    if (wasTap && calib.mode && !edit.mode) {
      const p = canvasPixelOfEvent(e);
      if (p) openCalibDialog(p[0], p[1]);
    } else if (wasTap && pick.mode && !edit.mode && !calib.mode) {
      // 取坐标模式：轻点 → 显示坐标
      doPick(e);
    } else if (wasTap && !edit.mode) {
      // 浏览模式：点地图上的徽章 → 弹出它是谁、在哪（只报信息，不改视图也不筛选）
      const hit = hitTestMarker(e.clientX, e.clientY);
      if (hit && hit.tid === LAYER_ENTRANCE_TID) {
        void showLayerEntrancePicker(hit);
      } else {
        showMarkTip(hit);
      }
    }
  }
  if (edit.mode && edit.painting && e.pointerId === edit.painting.id) {
    processPendingPaint();
    edit.painting = false;
    edit.lastCell = null;
    const grid = ed();
    if (grid) commitEditTransaction(grid);
    // 收笔时重算边界盒：涂出去或擦掉都会改变保存范围（也是"未知"计数的准头）
    updateEditStats();
    requestRender(RENDER_LAYERS.GRID | RENDER_LAYERS.SELECTION);
  }
  if (edit.mode && edit.marquee && e.pointerId === edit.marquee.id) {
    finishMarquee();
  }
  gestureRect = null;
};
canvasWrap.addEventListener("pointerup", endDrag);
canvasWrap.addEventListener("pointercancel", endDrag);
canvasWrap.addEventListener("wheel", (e) => {
  e.preventDefault();
  const rect = canvasWrap.getBoundingClientRect();
  // 按住画笔键时滚轮改画笔粗细，不改缩放——否则"按住 1 滚轮"和"按住 2 滚轮"
  // 会是两种毫不相干的行为。顺便用滚轮位置刷新 HUD，光标没动也能跟着。
  if (edit.mode && heldBrushKeys.size) {
    edit.pointer = { x: e.clientX - rect.left, y: e.clientY - rect.top };
    setBrushSize(edit.brushSize + (e.deltaY < 0 ? 1 : -1));
    return;
  }
  zoomAt(e.clientX - rect.left, e.clientY - rect.top, e.deltaY < 0 ? 1.15 : 1 / 1.15);
}, { passive: false });
canvasWrap.addEventListener("dblclick", () => { if (!calib.mode && !edit.mode) fitView(); });
canvasWrap.addEventListener("contextmenu", (e) => { if (edit.mode) e.preventDefault(); });
// HUD 是相对 canvasWrap 定位的，光标移出去就该收起来（移回来 pointermove 会再开）
canvasWrap.addEventListener("pointerleave", () => {
  if (heldBrushKeys.size) hideBrushHud();
  hideBrushCursor();
});

window.addEventListener("resize", () => {
  refreshUiMetrics();
  clampPan();
  applyView();
});
window.__NAV_GRID_EDITOR__ = {
  get document() { return ed(); },
  get editor() { return edit; },
  get linkEditor() { return link; },
  get floorMap() { return state.floorMap; },
  get floorOverlap() {
    return {
      key: state.floorOverlap.key,
      xzOnly: [...state.floorOverlap.xzOnly],
      threeD: [...state.floorOverlap.threeD],
      resolved: [...state.floorOverlap.resolved],
      components: state.floorOverlap.components,
    };
  },
  get floorPeerGridSizes() {
    return Object.fromEntries(
      [...state.floorPeerGrids].map(([key, value]) => [key, value.size])
    );
  },
  get calibration() {
    return { key: calib.key, hasMapping: !!calib.mapping };
  },
  refreshOverlaps: scheduleFloorOverlapLiveRefresh,
  get view() { return state.views.get(state.activeKey) || null; },
  setMode,
  get stats() {
    let tileCount = 0;
    let tileBytes = 0;
    for (const bitmap of tileCache.values()) {
      if (!isBitmap(bitmap)) continue;
      tileCount++;
      tileBytes += (bitmap.width || 0) * (bitmap.height || 0) * 4;
    }
    let overviewBytes = 0;
    let overviewCount = 0;
    for (const view of state.views.values()) {
      if (!view.overview) continue;
      overviewCount++;
      overviewBytes += (view.overview.width || 0) *
        (view.overview.height || 0) * 4;
    }
    const tierBytes = state.activeTier
      ? [state.activeTier.preview, state.activeTier.bitmap]
        .filter(Boolean)
        .reduce((sum, bitmap) => sum + (bitmap.width || 0) *
          (bitmap.height || 0) * 4, 0)
      : 0;
    const canvasBytes = [
      canvas, editCanvasEl, selCanvasEl, linkCanvasEl, pathCanvasEl,
    ].reduce((sum, item) => sum + item.width * item.height * 4, 0);
    return {
      activeKey: state.activeKey,
      activeTier: state.activeTier ? state.activeTier.id : null,
      tileCount,
      tileBytes,
      overviewCount,
      overviewBytes,
      tierBytes,
      canvasBytes,
      markerPoints: state.markers ? state.markers.points.length : 0,
      markupNodes: document.getElementsByTagName("*").length,
    };
  },
  undo: undoGridEdit,
  redo: redoGridEdit,
  render: requestRender,
  layers: RENDER_LAYERS,
};

window.addEventListener("load", () => {
  refreshUiMetrics();
  setSidePanel(false);   // 画布优先：检查器默认收起，模式轨始终保留
  syncModeChrome();      // 初始状态：浏览模式，只显示它的动作组与侧栏面板
  syncButtons();
  syncHistoryButtons();
  loadMarksStatus();
  loadPositionStatus();
  loadTileMaps();
  connect();
});
