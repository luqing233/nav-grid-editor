import { CELL_BLOCKED, CELL_FREE, CHUNK_SIZE } from "./grid-document.js";

const COLORS = {
  [CELL_FREE]: [35, 200, 175, 72],
  [CELL_BLOCKED]: [220, 68, 52, 96],
  [CELL_BLOCKED + 10]: [240, 118, 68, 58],
};

function parseChunkKey(key) {
  const comma = key.indexOf(",");
  return [Number(key.slice(0, comma)), Number(key.slice(comma + 1))];
}

function chunkCorners(cx, cz, transform) {
  const x0 = cx * CHUNK_SIZE;
  const z0 = cz * CHUNK_SIZE;
  const x1 = x0 + CHUNK_SIZE;
  const z1 = z0 + CHUNK_SIZE;
  const points = [
    [x0, z0],
    [x1, z0],
    [x0, z1],
    [x1, z1],
  ];
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const [x, z] of points) {
    const px = transform.a00 * x + transform.a01 * z + transform.a02;
    const py = transform.a10 * x + transform.a11 * z + transform.a12;
    if (px < minX) minX = px;
    if (px > maxX) maxX = px;
    if (py < minY) minY = py;
    if (py > maxY) maxY = py;
  }
  return { minX, minY, maxX, maxY };
}

export class GridRasterCache {
  constructor() {
    this.chunks = new Map();
  }

  clear() {
    this.chunks.clear();
  }

  sync(document) {
    const changed = [];
    for (const key of document.consumeDirtyChunks()) {
      const [cx, cz] = parseChunkKey(key);
      const chunk = document.chunks.get(key);
      if (!chunk || !chunk.filled.size) {
        this.chunks.delete(key);
        changed.push({
          key,
          cx,
          cz,
          baseX: cx * CHUNK_SIZE,
          baseZ: cz * CHUNK_SIZE,
          canvas: null,
        });
        continue;
      }
      const built = this.buildChunk(cx, cz, chunk);
      this.chunks.set(key, built);
      changed.push({ key, ...built });
    }
    return changed;
  }

  buildChunk(cx, cz, chunk) {
    const canvas = document.createElement("canvas");
    canvas.width = CHUNK_SIZE;
    canvas.height = CHUNK_SIZE;
    const context = canvas.getContext("2d");
    const image = context.createImageData(CHUNK_SIZE, CHUNK_SIZE);
    const pixels = image.data;
    const baseX = cx * CHUNK_SIZE;
    const baseZ = cz * CHUNK_SIZE;

    for (const index of chunk.filled) {
      const state = chunk.data[index];
      const ix = baseX + (index % CHUNK_SIZE);
      const iz = baseZ + Math.floor(index / CHUNK_SIZE);
      const color = state === CELL_BLOCKED
        ? COLORS[(ix + iz) & 1 ? CELL_BLOCKED : CELL_BLOCKED + 10]
        : COLORS[state];
      if (!color) continue;
      const pixel = index * 4;
      pixels[pixel] = color[0];
      pixels[pixel + 1] = color[1];
      pixels[pixel + 2] = color[2];
      pixels[pixel + 3] = color[3];
    }

    context.putImageData(image, 0, 0);
    return { cx, cz, canvas, baseX, baseZ };
  }

  drawChunk(context, chunk, transform) {
    if (!chunk.canvas) return;
    const ox = transform.a00 * chunk.baseX + transform.a01 * chunk.baseZ + transform.a02;
    const oy = transform.a10 * chunk.baseX + transform.a11 * chunk.baseZ + transform.a12;
    context.save();
    context.imageSmoothingEnabled = false;
    context.transform(
      transform.a00,
      transform.a10,
      transform.a01,
      transform.a11,
      ox,
      oy,
    );
    context.drawImage(chunk.canvas, 0, 0);
    context.restore();
  }

  drawAll(context, transform, visible) {
    for (const chunk of this.chunks.values()) {
      const bounds = chunkCorners(chunk.cx, chunk.cz, transform);
      if (bounds.maxX < visible.x || bounds.minX > visible.x + visible.w) continue;
      if (bounds.maxY < visible.y || bounds.minY > visible.y + visible.h) continue;
      this.drawChunk(context, chunk, transform);
    }
  }

  drawChanged(context, transform, visible, changed) {
    for (const chunk of changed) {
      const bounds = chunkCorners(chunk.cx, chunk.cz, transform);
      if (bounds.maxX < visible.x || bounds.minX > visible.x + visible.w) continue;
      if (bounds.maxY < visible.y || bounds.minY > visible.y + visible.h) continue;
      context.save();
      context.clearRect(
        bounds.minX - 1,
        bounds.minY - 1,
        bounds.maxX - bounds.minX + 2,
        bounds.maxY - bounds.minY + 2,
      );
      context.restore();
      this.drawChunk(context, chunk, transform);
    }
  }
}
