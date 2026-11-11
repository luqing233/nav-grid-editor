import { CELL_FREE, CELL_UNKNOWN } from "./grid-document.js";

export const CAPTURE_FREE_MAX_SPEED = 7.5;
export const CAPTURE_FREE_MAX_GAP_METERS = 7;

function validPoint(point) {
  return !!point && Number.isFinite(Number(point.x)) && Number.isFinite(Number(point.z));
}

function walkGridLine(x0, z0, x1, z1, bridgeDiagonal, visit) {
  let x = x0;
  let z = z0;
  const dx = Math.abs(x1 - x0);
  const dz = Math.abs(z1 - z0);
  const sx = x0 < x1 ? 1 : -1;
  const sz = z0 < z1 ? 1 : -1;
  let error = dx - dz;
  while (true) {
    visit(x, z);
    if (x === x1 && z === z1) return;
    const oldX = x;
    const oldZ = z;
    const doubled = error * 2;
    if (doubled > -dz) {
      error -= dz;
      x += sx;
    }
    if (doubled < dx) {
      error += dx;
      z += sz;
    }
    // Radius=0 must not leave a diagonal-only connection between path cells.
    if (bridgeDiagonal && x !== oldX && z !== oldZ) visit(x, oldZ);
  }
}

export function paintCaptureFreeSegment(document, from, to, radius = 0) {
  if (!document || !validPoint(from) || !validPoint(to)) {
    return { connected: false, changed: false, distance: NaN };
  }
  const dx = Number(to.x) - Number(from.x);
  const dz = Number(to.z) - Number(from.z);
  const distance = Math.hypot(dx, dz);
  if (!Number.isFinite(distance) || distance > CAPTURE_FREE_MAX_GAP_METERS) {
    return { connected: false, changed: false, distance };
  }
  const originX = Number(document.origin?.[0]);
  const originZ = Number(document.origin?.[1]);
  const cellSize = Number(document.cellSize);
  if (!Number.isFinite(originX) || !Number.isFinite(originZ) ||
      !Number.isFinite(cellSize) || cellSize <= 0) {
    return { connected: false, changed: false, distance };
  }

  const size = Math.max(0, Math.min(50, Math.floor(Number(radius) || 0)));
  const startX = Math.floor((Number(from.x) - originX) / cellSize);
  const startZ = Math.floor((Number(from.z) - originZ) / cellSize);
  const endX = Math.floor((Number(to.x) - originX) / cellSize);
  const endZ = Math.floor((Number(to.z) - originZ) / cellSize);
  const touched = new Set();
  let changed = false;
  const paint = (ix, iz) => {
    for (let ox = -size; ox <= size; ox++) {
      for (let oz = -size; oz <= size; oz++) {
        const x = ix + ox;
        const z = iz + oz;
        const key = x + "," + z;
        if (touched.has(key)) continue;
        touched.add(key);
        if (document.getCell(x, z) !== CELL_UNKNOWN) continue;
        if (document.setCell(x, z, CELL_FREE)) changed = true;
      }
    }
  };
  walkGridLine(startX, startZ, endX, endZ, size === 0, paint);
  return { connected: true, changed, distance, cells: touched.size };
}
