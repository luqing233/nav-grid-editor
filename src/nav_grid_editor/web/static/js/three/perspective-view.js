// 透视视图：把当前地图、各分层图片和它们的导航网格放进一个可环绕的 3D 场景。
//
// 坐标约定：游戏世界是 X/Z 平面 + Y 高度；three 默认 Y 轴朝上，所以
// world(x, z, y) → three(x, y, z)，不需要额外换轴。
//
// 每个分层的图片严格映射到目录里的 bounds（west/east/south/north）矩形，
// 大地图则用标定矩阵把总图像素四角映射到世界，二者都能得到真实的 X/Z 位置。
import * as THREE from "three";
import { OrbitControls } from "../vendor/three/OrbitControls.js";

const FREE_COLOR = 0x23c8af;
const BLOCK_COLOR = 0xdc4434;
const MARK_COLOR = 0x46beff;
const LINK_COLOR = 0xf0c66c;
const MAIN_TIER = "__main__";
const MAX_CELL_INSTANCES = 220000;

function qs(params) {
  return new URLSearchParams(params).toString();
}

function affine(m, px, py) {
  return [
    m[0][0] * px + m[0][1] * py + m[0][2],
    m[1][0] * px + m[1][1] * py + m[1][2],
  ];
}

function fitSize(w, h, max) {
  const scale = Math.min(1, max / Math.max(w, h));
  return [
    Math.max(1, Math.round(w * scale)),
    Math.max(1, Math.round(h * scale)),
  ];
}

// 图片先缩到 maxSize 再上传 GPU：官方分层原图 4200×3600，直接铺进去几张
// 就是几百 MB 显存，缩到 1536 长边后肉眼几乎看不出差别。
async function decodeBitmap(url, maxSize) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error("HTTP " + resp.status);
  const blob = await resp.blob();
  if (typeof createImageBitmap === "function") {
    const probe = await createImageBitmap(blob);
    const [w, h] = fitSize(probe.width, probe.height, maxSize);
    if (w === probe.width && h === probe.height) return probe;
    const scaled = await createImageBitmap(blob, {
      resizeWidth: w, resizeHeight: h, resizeQuality: "medium",
    });
    if (typeof probe.close === "function") probe.close();
    return scaled;
  }
  const objectUrl = URL.createObjectURL(blob);
  try {
    const img = await new Promise((resolve, reject) => {
      const el = new Image();
      el.onload = () => resolve(el);
      el.onerror = () => reject(new Error("图片解码失败"));
      el.src = objectUrl;
    });
    const [w, h] = fitSize(img.naturalWidth, img.naturalHeight, maxSize);
    const canvas = document.createElement("canvas");
    canvas.width = w;
    canvas.height = h;
    canvas.getContext("2d").drawImage(img, 0, 0, w, h);
    return canvas;
  } finally {
    URL.revokeObjectURL(objectUrl);
  }
}

function makeTexture(source) {
  const texture = new THREE.Texture(source);
  texture.flipY = false;
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.generateMipmaps = false;
  texture.minFilter = THREE.LinearFilter;
  texture.magFilter = THREE.LinearFilter;
  texture.anisotropy = 4;
  texture.needsUpdate = true;
  return texture;
}

function releaseTexture(texture) {
  if (!texture) return;
  const source = texture.image;
  texture.dispose();
  if (source && typeof source.close === "function") source.close();
}

// 四个角按「图片左上 → 右上 → 右下 → 左下」给，UV 与 flipY=false 对齐。
function quadMesh(corners, y, material) {
  const positions = [];
  for (const [x, z] of corners) positions.push(x, y, z);
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute(
    "position", new THREE.Float32BufferAttribute(positions, 3));
  geometry.setAttribute("uv", new THREE.Float32BufferAttribute(
    [0, 0, 1, 0, 1, 1, 0, 1], 2));
  geometry.setIndex([0, 1, 2, 0, 2, 3]);
  return new THREE.Mesh(geometry, material);
}

function cellMesh(cells, originX, originZ, cellSize, y, color, thickness) {
  if (!cells || !cells.length) return null;
  const mesh = new THREE.InstancedMesh(
    new THREE.BoxGeometry(1, 1, 1),
    new THREE.MeshLambertMaterial({
      color, transparent: true, opacity: 0.95,
    }),
    cells.length,
  );
  const matrix = new THREE.Matrix4();
  const quaternion = new THREE.Quaternion();
  const scale = new THREE.Vector3(
    cellSize * 0.92, thickness, cellSize * 0.92);
  const position = new THREE.Vector3();
  for (let i = 0; i < cells.length; i++) {
    const ix = Number(cells[i][0]);
    const iz = Number(cells[i][1]);
    position.set(
      originX + (ix + 0.5) * cellSize,
      y + thickness / 2,
      originZ + (iz + 0.5) * cellSize,
    );
    matrix.compose(position, quaternion, scale);
    mesh.setMatrixAt(i, matrix);
  }
  mesh.instanceMatrix.needsUpdate = true;
  mesh.frustumCulled = false;
  return mesh;
}

function regionBox(region, y, exaggeration) {
  const width = Math.max(0.5, Number(region.x1) - Number(region.x0));
  const depth = Math.max(0.5, Number(region.z1) - Number(region.z0));
  const span = Math.max(0, Number(region.y_max) - Number(region.y_min));
  const height = Math.max(3, span * exaggeration);
  const mesh = new THREE.Mesh(
    new THREE.BoxGeometry(width, height, depth),
    new THREE.MeshLambertMaterial({
      color: MARK_COLOR, transparent: true, opacity: 0.3, depthWrite: false,
    }),
  );
  mesh.position.set(
    (Number(region.x0) + Number(region.x1)) / 2,
    y + height / 2,
    (Number(region.z0) + Number(region.z1)) / 2,
  );
  const edges = new THREE.LineSegments(
    new THREE.EdgesGeometry(mesh.geometry),
    new THREE.LineBasicMaterial({ color: MARK_COLOR }),
  );
  mesh.add(edges);
  return mesh;
}

export class PerspectiveView {
  constructor(root) {
    this.root = root;
    this.canvas = root.querySelector("#pvCanvas");
    this.labelLayer = root.querySelector("#pvLabels");
    this.statusEl = root.querySelector("#pvStatus");
    this.groupSelect = root.querySelector("#pvGroup");
    this.spacingInput = root.querySelector("#pvSpacing");
    this.toggles = {
      base: root.querySelector("#pvBase"),
      layers: root.querySelector("#pvLayers"),
      grid: root.querySelector("#pvGrid"),
      marks: root.querySelector("#pvMarks"),
      links: root.querySelector("#pvLinks"),
    };
    this.open = false;
    this.ready = false;
    this.map = "";
    this.zoom = "";
    this.tier = "";
    this.layers = [];
    this.floorLayers = new Map();
    this.links = [];
    this.calib = null;
    this.groupKey = "";
    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0x0b0f13);
    this.plateGroup = new THREE.Group();
    this.scene.add(this.plateGroup);
    this.plateYs = new Map();
    this.labels = new Map();
    this.pendingTextures = [];
    this.camera = new THREE.PerspectiveCamera(52, 1, 0.5, 20000);
    this.renderer = new THREE.WebGLRenderer({
      canvas: this.canvas, antialias: true,
    });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.controls = new OrbitControls(this.camera, this.canvas);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.08;
    this.controls.maxPolarAngle = Math.PI * 0.495;
    this.controls.screenSpacePanning = false;
    this.buildStaticScene();
    this.bindEvents();
  }

  buildStaticScene() {
    const hemi = new THREE.HemisphereLight(0xdcecff, 0x10161d, 2.4);
    this.scene.add(hemi);
    const sun = new THREE.DirectionalLight(0xffffff, 2.6);
    sun.position.set(0.6, 1, 0.4).multiplyScalar(2000);
    this.scene.add(sun);
  }

  bindEvents() {
    this.controls.addEventListener("change", () => this.syncLabels());
    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(this.root);
    this.groupSelect.addEventListener("change", () => {
      this.groupKey = this.groupSelect.value;
      this.rebuildPlates();
    });
    this.spacingInput.addEventListener("input", () => this.rebuildPlates());
    this.root.querySelector("#pvReset").addEventListener("click", () => {
      this.frameScene();
    });
    for (const toggle of Object.values(this.toggles)) {
      toggle.addEventListener("change", () => this.applyVisibility());
    }
  }

  resize() {
    if (!this.open) return;
    const width = this.root.clientWidth || 1;
    const height = this.root.clientHeight || 1;
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(width, height, false);
  }

  setStatus(text) {
    if (this.statusEl) this.statusEl.textContent = text || "";
  }

  async show({ map, zoom, tier }) {
    this.open = true;
    this.root.hidden = false;
    this.resize();
    this.map = map;
    this.zoom = zoom;
    this.tier = tier || "";
    this.setStatus("正在加载地图与网格…");
    try {
      await this.loadData();
      this.spacingInput.value = String(this.autoSpacing());
      await this.rebuildPlates();
      this.frameScene();
      this.setStatus("");
    } catch (e) {
      this.setStatus("加载失败: " + (e && e.message ? e.message : e));
    }
    this.renderer.setAnimationLoop(() => {
      this.controls.update();
      this.renderer.render(this.scene, this.camera);
    });
  }

  async loadData() {
    const [calib, layersRes, floorsRes, linksRes] = await Promise.all([
      fetch("/api/calib?" + qs({ map: this.map, zoom: this.zoom }))
        .then(r => r.json()),
      fetch("/api/layers?" + qs({ map: this.map, zoom: this.zoom }))
        .then(r => r.json()),
      fetch("/api/grid2d/floor-map?" +
            qs({ map: this.map, zoom: this.zoom, level_id: "" }))
        .then(r => r.json()),
      fetch("/api/grid-links?" + qs({ map: this.map })).then(r => r.json()),
    ]);
    this.calib = calib && calib.matrix ? calib : null;
    this.layers = Array.isArray(layersRes.layers) ? layersRes.layers : [];
    this.floorLayers = new Map(
      (floorsRes.layers || []).map(item => [item.tier_id, item]));
    this.links = Array.isArray(linksRes.links) ? linksRes.links : [];
    this.fillGroupSelect();
  }

  fillGroupSelect() {
    const groups = new Map();
    for (const layer of this.layers) {
      const key = layer.group_key || layer.group || layer.id;
      if (!groups.has(key)) {
        groups.set(key, {
          key,
          name: layer.group || layer.name || key,
          level: layer.level_name || layer.level_id || "",
          layers: [],
        });
      }
      groups.get(key).layers.push(layer);
    }
    const tierLayer = this.layers.find(item => item.id === this.tier);
    let preferred = tierLayer
      ? (tierLayer.group_key || tierLayer.group)
      : "";
    if (!preferred) {
      const withGrid = this.layers.find(item => {
        const floor = this.floorLayers.get(item.id);
        return floor && floor.has_grid;
      });
      preferred = withGrid
        ? (withGrid.group_key || withGrid.group) : "";
    }
    this.groups = [...groups.values()].filter(group => group.layers.length);
    this.groupSelect.replaceChildren();
    for (const group of this.groups) {
      const option = document.createElement("option");
      option.value = group.key;
      const label = (group.level ? group.level + " · " : "") + group.name +
        "（" + group.layers.length + " 层）";
      option.textContent = label;
      this.groupSelect.appendChild(option);
    }
    this.groupKey = this.groups.some(group => group.key === preferred)
      ? preferred
      : (this.groups[0] ? this.groups[0].key : "");
    this.groupSelect.value = this.groupKey;
  }

  currentGroup() {
    return this.groups.find(group => group.key === this.groupKey) || null;
  }

  spacing() {
    const value = Number(this.spacingInput.value);
    return Number.isFinite(value) ? value : 45;
  }

  // 垂直间距按地图尺度自适应：不同 zoom 的世界跨度差一个数量级，
  // 固定像素值不是挤成一张纸就是飞出画面。
  autoSpacing() {
    let span = 0;
    if (this.calib) {
      const size = this.calib.image_size || {};
      const width = Number(size.width) || 0;
      const height = Number(size.height) || 0;
      if (width > 0 && height > 0) {
        const corners = [
          affine(this.calib.matrix, 0, 0),
          affine(this.calib.matrix, width, 0),
          affine(this.calib.matrix, width, height),
          affine(this.calib.matrix, 0, height),
        ];
        const xs = corners.map(point => point[0]);
        const zs = corners.map(point => point[1]);
        span = Math.max(
          Math.max(...xs) - Math.min(...xs),
          Math.max(...zs) - Math.min(...zs));
      }
    }
    // 分层图本身的尺寸才是"要摊开多少"的关键尺度，比整张大地图更有参考性。
    let layerSpan = 0;
    for (const layer of this.layers) {
      const bounds = layer.bounds || {};
      const w = Number(bounds.east) - Number(bounds.west);
      const d = Number(bounds.north) - Number(bounds.south);
      if (!Number.isFinite(w) || !Number.isFinite(d)) continue;
      layerSpan = Math.max(layerSpan, Math.hypot(w, d));
      if (!(span > 0)) span = Math.max(span, w, d);
    }
    const scale = Math.max(span, layerSpan) || 1000;
    return Math.max(15, Math.min(400, Math.round(scale * 0.05)));
  }

  // 分层统一往大地图上方铺开（真实的分层在地下，但往下压会被底图完全挡
  // 住），组内按官方 tier_index 从低到高排序，越高越靠上。
  groupPlateYs(group) {
    const spacing = this.spacing();
    const ordered = [...group.layers].sort((a, b) => {
      const ta = Number(a.tier_index);
      const tb = Number(b.tier_index);
      return (Number.isFinite(ta) ? ta : 0) - (Number.isFinite(tb) ? tb : 0);
    });
    const map = new Map();
    ordered.forEach((layer, index) => {
      map.set(layer.id, (index + 1) * spacing);
    });
    return map;
  }

  clearPlates() {
    this.plateGroup.traverse(object => {
      if (object.geometry) object.geometry.dispose();
      if (object.material) {
        const materials = Array.isArray(object.material)
          ? object.material : [object.material];
        for (const material of materials) material.dispose();
      }
    });
    this.plateGroup.clear();
    for (const texture of this.pendingTextures) releaseTexture(texture);
    this.pendingTextures = [];
    this.plateYs = new Map();
    this.labelLayer.replaceChildren();
    this.labels = new Map();
  }

  async rebuildPlates() {
    if (!this.open) return;
    this.clearPlates();
    const group = this.currentGroup();
    const yMap = group ? this.groupPlateYs(group) : new Map();
    const tasks = [];
    if (this.calib) tasks.push(this.buildBasePlate());
    if (group) {
      for (const layer of group.layers) {
        tasks.push(this.buildLayerPlate(layer, yMap.get(layer.id) || 0));
      }
    }
    const linkY = tier => {
      if (!tier) return 0;
      return this.plateYs.get(tier) || 0;
    };
    await Promise.all(tasks);
    if (this.toggles.links.checked) this.buildLinks(linkY);
    this.applyVisibility();
    this.frameScene(true);
  }

  async buildBasePlate() {
    const size = this.calib.image_size || {};
    const width = Number(size.width);
    const height = Number(size.height);
    if (!(width > 0) || !(height > 0)) return;
    const matrix = this.calib.matrix;
    const corners = [
      affine(matrix, 0, 0),
      affine(matrix, width, 0),
      affine(matrix, width, height),
      affine(matrix, 0, height),
    ];
    const y = 0;
    this.plateYs.set(MAIN_TIER, y);
    let mesh = null;
    try {
      const texture = makeTexture(await decodeBitmap(
        "/api/overview?" + qs({ map: this.map, zoom: this.zoom }), 2048));
      this.pendingTextures.push(texture);
      mesh = quadMesh(corners, y, new THREE.MeshBasicMaterial({
        map: texture, transparent: true, opacity: 0.94,
        depthWrite: false, side: THREE.DoubleSide,
      }));
      mesh.name = "base-plate";
      mesh.userData.kind = "base";
      this.plateGroup.add(mesh);
    } catch (e) {
      this.setStatus("底图缩略图加载失败，只显示网格");
    }
    if (mesh) {
      const cx = corners.reduce((sum, p) => sum + p[0], 0) / 4;
      const cz = corners.reduce((sum, p) => sum + p[1], 0) / 4;
      this.addLabel(MAIN_TIER, "大地图", cx, y, cz);
    }
    await this.buildGridFor(MAIN_TIER, corners, y);
    if (this.toggles.marks.checked) this.buildMarks(MAIN_TIER, y);
  }

  async buildLayerPlate(layer, y) {
    const bounds = layer.bounds || {};
    const west = Number(bounds.west);
    const east = Number(bounds.east);
    const south = Number(bounds.south);
    const north = Number(bounds.north);
    if (![west, east, south, north].every(Number.isFinite)) return;
    this.plateYs.set(layer.id, y);
    const corners = [
      [west, north], [east, north], [east, south], [west, south],
    ];
    const mesh = quadMesh(corners, y, new THREE.MeshBasicMaterial({
      color: 0xffffff, transparent: true, opacity: 0.001,
      depthWrite: false, side: THREE.DoubleSide,
    }));
    mesh.name = "layer-plate:" + layer.id;
    mesh.userData.kind = "layer";
    mesh.userData.tierId = layer.id;
    this.plateGroup.add(mesh);
    try {
      const texture = makeTexture(await decodeBitmap(
        "/api/layer-image?" +
        qs({ map: this.map, zoom: this.zoom, tier: layer.id }), 1536));
      this.pendingTextures.push(texture);
      mesh.material.map = texture;
      mesh.material.opacity = 0.9;
      mesh.material.needsUpdate = true;
    } catch (e) {
      // 拿不到官方图也要保留网格，所以这里只降级不抛错。
    }
    this.addLabel(
      layer.id, layer.name || layer.id,
      (west + east) / 2, y, (south + north) / 2);
    await this.buildGridFor(layer.id, corners, y);
    if (this.toggles.marks.checked) this.buildMarks(layer.id, y);
  }

  async buildGridFor(tierId, corners, y) {
    const xs = corners.map(point => point[0]);
    const zs = corners.map(point => point[1]);
    const tier = tierId === MAIN_TIER ? "" : tierId;
    let res;
    try {
      res = await fetch("/api/grid2d?" +
        qs({ map: this.map, zoom: this.zoom, tier })).then(r => r.json());
    } catch (e) {
      return;
    }
    const data = res && res.data;
    if (!data) return;
    const cells = data.cells || [];
    const blocked = data.blocked || [];
    if (cells.length + blocked.length > MAX_CELL_INSTANCES) {
      this.setStatus("网格规模过大，已跳过 " + tierId + " 的格子");
      return;
    }
    const origin = data.origin || [0, 0, 0];
    const cellSize = Number(data.cell_size) || 1;
    const span = Math.max(
      Math.max(...xs) - Math.min(...xs),
      Math.max(...zs) - Math.min(...zs));
    const thickness = Math.max(cellSize * 0.6, span / 900);
    const free = cellMesh(cells, origin[0], origin[2], cellSize, y,
      FREE_COLOR, thickness);
    const block = cellMesh(blocked, origin[0], origin[2], cellSize, y,
      BLOCK_COLOR, thickness);
    for (const mesh of [free, block]) {
      if (!mesh) continue;
      mesh.name = "grid:" + tierId;
      mesh.userData.kind = "grid";
      mesh.userData.tierId = tierId;
      this.plateGroup.add(mesh);
    }
  }

  buildMarks(tierId, y) {
    const floor = this.floorLayers.get(tierId);
    if (!floor || !floor.floor_regions || !floor.floor_regions.length) return;
    for (const region of floor.floor_regions) {
      const box = regionBox(region, y, this.spacing() / 6);
      box.userData.kind = "marks";
      box.userData.tierId = tierId;
      this.plateGroup.add(box);
    }
  }

  buildLinks(linkY) {
    for (const link of this.links) {
      const from = link.from || {};
      const to = link.to || {};
      const a = from.rect || {};
      const b = to.rect || {};
      const ax = (Number(a.x0) + Number(a.x1)) / 2;
      const az = (Number(a.z0) + Number(a.z1)) / 2;
      const bx = (Number(b.x0) + Number(b.x1)) / 2;
      const bz = (Number(b.z0) + Number(b.z1)) / 2;
      if (![ax, az, bx, bz].every(Number.isFinite)) continue;
      const ay = linkY(from.tier || MAIN_TIER);
      const by = linkY(to.tier || MAIN_TIER);
      const geometry = new THREE.BufferGeometry().setFromPoints([
        new THREE.Vector3(ax, ay, az),
        new THREE.Vector3(bx, by, bz),
      ]);
      const line = new THREE.Line(
        geometry, new THREE.LineBasicMaterial({ color: LINK_COLOR }));
      line.userData.kind = "links";
      this.plateGroup.add(line);
    }
  }

  addLabel(key, text, x, y, z) {
    const el = document.createElement("div");
    el.className = "pv-label";
    el.textContent = text;
    this.labelLayer.appendChild(el);
    this.labels.set(key, { el, position: new THREE.Vector3(x, y, z) });
  }

  applyVisibility() {
    const showBase = this.toggles.base.checked;
    const showLayers = this.toggles.layers.checked;
    const showGrid = this.toggles.grid.checked;
    const showMarks = this.toggles.marks.checked;
    const showLinks = this.toggles.links.checked;
    this.plateGroup.traverse(object => {
      const kind = object.userData && object.userData.kind;
      if (!kind) return;
      if (kind === "base") object.visible = showBase;
      else if (kind === "layer") object.visible = showLayers;
      else if (kind === "grid") object.visible = showGrid;
      else if (kind === "marks") object.visible = showMarks;
      else if (kind === "links") object.visible = showLinks;
    });
    for (const { el } of this.labels.values()) el.hidden = !showBase;
    this.syncLabels();
  }

  sceneBounds() {
    const box = new THREE.Box3();
    let has = false;
    this.plateGroup.traverse(object => {
      if (!object.isMesh || !object.userData.kind) return;
      if (object.userData.kind !== "base" &&
          object.userData.kind !== "layer") return;
      object.geometry.computeBoundingBox();
      const local = object.geometry.boundingBox;
      if (!local) return;
      box.union(local);
      has = true;
    });
    if (!has) return null;
    return box;
  }

  frameScene(keepAngles = false) {
    const box = this.sceneBounds();
    if (!box) return;
    const center = box.getCenter(new THREE.Vector3());
    const size = box.getSize(new THREE.Vector3());
    const radius = Math.max(size.x, size.z) * 0.5 || 100;
    if (!keepAngles || !this.framed) {
      this.camera.position.set(
        center.x, center.y + radius * 1.05, center.z + radius * 1.15);
      this.framed = true;
    }
    this.controls.target.copy(center);
    this.camera.near = Math.max(0.5, radius / 400);
    this.camera.far = radius * 40;
    this.camera.updateProjectionMatrix();
    this.controls.update();
    this.syncLabels();
  }

  syncLabels() {
    if (!this.open) return;
    const width = this.root.clientWidth || 1;
    const height = this.root.clientHeight || 1;
    const showBase = this.toggles.base.checked;
    const showLayers = this.toggles.layers.checked;
    for (const [key, label] of this.labels) {
      const isBase = key === MAIN_TIER;
      if (isBase ? !showBase : !showLayers) {
        label.el.style.display = "none";
        continue;
      }
      const projected = label.position.clone().project(this.camera);
      if (projected.z > 1) {
        label.el.style.display = "none";
        continue;
      }
      label.el.style.display = "";
      label.el.style.transform =
        "translate(-50%, -50%) translate(" +
        ((projected.x * 0.5 + 0.5) * width).toFixed(1) + "px," +
        ((-projected.y * 0.5 + 0.5) * height).toFixed(1) + "px)";
    }
  }

  close() {
    this.open = false;
    this.renderer.setAnimationLoop(null);
    this.clearPlates();
    this.root.hidden = true;
    this.ready = false;
    this.framed = false;
  }
}
