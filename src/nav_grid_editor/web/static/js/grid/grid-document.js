export const CHUNK_SIZE = 256;
export const CELL_UNKNOWN = 0;
export const CELL_FREE = 1;
export const CELL_BLOCKED = 2;

const CHUNK_AREA = CHUNK_SIZE * CHUNK_SIZE;
const HISTORY_LIMIT = 100;

function floorDiv(value, divisor) {
  return Math.floor(value / divisor);
}

function positiveMod(value, divisor) {
  return ((value % divisor) + divisor) % divisor;
}

function chunkKey(cx, cz) {
  return cx + "," + cz;
}

function cellKey(ix, iz) {
  return ix + "," + iz;
}

function createChunk() {
  return {
    data: new Uint8Array(CHUNK_AREA),
    filled: new Set(),
  };
}

export class GridDocument {
  constructor({ origin = [0, 0], shape = null, cellSize = 1 } = {}) {
    this.origin = [Number(origin[0]) || 0, Number(origin[1]) || 0];
    this.shape = shape ? [Number(shape[0]), Number(shape[1])] : null;
    this.cellSize = Number(cellSize) || 1;
    this.chunks = new Map();
    this.dirtyChunks = new Set();
    this.freeCount = 0;
    this.blockedCount = 0;
    this.bbox = null;
    this.derivedDirty = false;
    this.revision = 0;
    this.history = { undo: [], redo: [], active: null };
    this.selRect = null;
    this.loaded = false;
  }

  static fromSparse({ origin, shape, cellSize, cells = [], blocked = [] }) {
    const document = new GridDocument({ origin, shape, cellSize });
    document.setCells(cells, CELL_FREE);
    document.setCells(blocked, CELL_BLOCKED);
    return document;
  }

  get size() {
    return this.freeCount + this.blockedCount;
  }

  getCell(ix, iz) {
    const cx = floorDiv(ix, CHUNK_SIZE);
    const cz = floorDiv(iz, CHUNK_SIZE);
    const chunk = this.chunks.get(chunkKey(cx, cz));
    if (!chunk) return CELL_UNKNOWN;
    const lx = positiveMod(ix, CHUNK_SIZE);
    const lz = positiveMod(iz, CHUNK_SIZE);
    return chunk.data[lz * CHUNK_SIZE + lx];
  }

  setCell(ix, iz, value, { record = true } = {}) {
    if (!Number.isInteger(ix) || !Number.isInteger(iz)) return false;
    if (value !== CELL_UNKNOWN && value !== CELL_FREE && value !== CELL_BLOCKED) {
      throw new RangeError("invalid cell state: " + value);
    }
    const oldValue = this.getCell(ix, iz);
    if (oldValue === value) return false;

    const cx = floorDiv(ix, CHUNK_SIZE);
    const cz = floorDiv(iz, CHUNK_SIZE);
    const key = chunkKey(cx, cz);
    let chunk = this.chunks.get(key);
    if (!chunk) {
      if (value === CELL_UNKNOWN) return false;
      chunk = createChunk();
      this.chunks.set(key, chunk);
    }

    const lx = positiveMod(ix, CHUNK_SIZE);
    const lz = positiveMod(iz, CHUNK_SIZE);
    const index = lz * CHUNK_SIZE + lx;

    if (record && this.history.active && !this.history.active.cells.has(cellKey(ix, iz))) {
      this.history.active.cells.set(cellKey(ix, iz), { ix, iz, before: oldValue });
    }

    chunk.data[index] = value;
    if (value === CELL_UNKNOWN) chunk.filled.delete(index);
    else chunk.filled.add(index);

    if (oldValue === CELL_FREE) this.freeCount--;
    else if (oldValue === CELL_BLOCKED) this.blockedCount--;
    if (value === CELL_FREE) this.freeCount++;
    else if (value === CELL_BLOCKED) this.blockedCount++;

    this.dirtyChunks.add(key);
    this.revision++;

    if (value === CELL_UNKNOWN) {
      this.derivedDirty = true;
    } else {
      if (!this.bbox) this.bbox = { x0: ix, x1: ix, z0: iz, z1: iz };
      else {
        if (ix < this.bbox.x0) this.bbox.x0 = ix;
        if (ix > this.bbox.x1) this.bbox.x1 = ix;
        if (iz < this.bbox.z0) this.bbox.z0 = iz;
        if (iz > this.bbox.z1) this.bbox.z1 = iz;
      }
    }

    if (!chunk.filled.size) this.chunks.delete(key);
    return true;
  }

  setCells(entries, state) {
    for (const entry of entries || []) {
      this.setCell(Number(entry[0]), Number(entry[1]), state, { record: false });
    }
  }

  recomputeDerived() {
    let bbox = null;
    for (const [key, chunk] of this.chunks) {
      if (!chunk.filled.size) {
        this.chunks.delete(key);
        continue;
      }
      const comma = key.indexOf(",");
      const cx = Number(key.slice(0, comma));
      const cz = Number(key.slice(comma + 1));
      const baseX = cx * CHUNK_SIZE;
      const baseZ = cz * CHUNK_SIZE;
      for (const index of chunk.filled) {
        const ix = baseX + (index % CHUNK_SIZE);
        const iz = baseZ + Math.floor(index / CHUNK_SIZE);
        if (!bbox) bbox = { x0: ix, x1: ix, z0: iz, z1: iz };
        else {
          if (ix < bbox.x0) bbox.x0 = ix;
          if (ix > bbox.x1) bbox.x1 = ix;
          if (iz < bbox.z0) bbox.z0 = iz;
          if (iz > bbox.z1) bbox.z1 = iz;
        }
      }
    }
    this.bbox = bbox;
    this.derivedDirty = false;
    return bbox;
  }

  getBBox() {
    if (this.derivedDirty && !this.history.active) this.recomputeDerived();
    return this.bbox;
  }

  savedBounds() {
    const bbox = this.getBBox();
    if (!this.shape) return bbox;
    const x1 = this.shape[1] - 1;
    const z1 = this.shape[0] - 1;
    if (!bbox) return { x0: 0, x1, z0: 0, z1 };
    return {
      x0: Math.min(0, bbox.x0),
      x1: Math.max(x1, bbox.x1),
      z0: Math.min(0, bbox.z0),
      z1: Math.max(z1, bbox.z1),
    };
  }

  forEachChunk(callback) {
    for (const [key, chunk] of this.chunks) {
      const comma = key.indexOf(",");
      const cx = Number(key.slice(0, comma));
      const cz = Number(key.slice(comma + 1));
      callback(cx, cz, chunk);
    }
  }

  forEachCell(range, callback) {
    const visit = callback || range;
    const bounds = callback ? range : null;
    for (const [key, chunk] of this.chunks) {
      if (!chunk.filled.size) continue;
      const comma = key.indexOf(",");
      const cx = Number(key.slice(0, comma));
      const cz = Number(key.slice(comma + 1));
      const baseX = cx * CHUNK_SIZE;
      const baseZ = cz * CHUNK_SIZE;
      if (bounds) {
        if (baseX > bounds.x1 || baseX + CHUNK_SIZE - 1 < bounds.x0) continue;
        if (baseZ > bounds.z1 || baseZ + CHUNK_SIZE - 1 < bounds.z0) continue;
      }
      for (const index of chunk.filled) {
        const ix = baseX + (index % CHUNK_SIZE);
        const iz = baseZ + Math.floor(index / CHUNK_SIZE);
        if (bounds && (ix < bounds.x0 || ix > bounds.x1 || iz < bounds.z0 || iz > bounds.z1)) {
          continue;
        }
        visit(ix, iz, chunk.data[index]);
      }
    }
  }

  exportSparse() {
    const cells = [];
    const blocked = [];
    this.forEachCell((ix, iz, value) => {
      if (value === CELL_FREE) cells.push([ix, iz]);
      else if (value === CELL_BLOCKED) blocked.push([ix, iz]);
    });
    const compare = (a, b) => a[0] - b[0] || a[1] - b[1];
    cells.sort(compare);
    blocked.sort(compare);
    return { cells, blocked };
  }

  applyMetadata(meta) {
    if (!meta) return;
    this.origin = [meta.origin[0], meta.origin[1]];
    this.shape = meta.shape ? [meta.shape[0], meta.shape[1]] : null;
    this.cellSize = meta.cellSize;
  }

  metadataSnapshot() {
    return {
      origin: [this.origin[0], this.origin[1]],
      shape: this.shape ? [this.shape[0], this.shape[1]] : null,
      cellSize: this.cellSize,
    };
  }

  beginTransaction(metaBefore = null) {
    if (this.history.active) return this.history.active;
    this.history.active = { cells: new Map(), metaBefore };
    return this.history.active;
  }

  commitTransaction(metaAfter = null) {
    const active = this.history.active;
    this.history.active = null;
    if (this.derivedDirty) this.recomputeDerived();
    if (!active) return false;
    const changes = [];
    for (const { ix, iz, before } of active.cells.values()) {
      const after = this.getCell(ix, iz);
      if (before !== after) changes.push([ix, iz, before, after]);
    }
    const beforeMeta = active.metaBefore;
    const afterMeta = beforeMeta ? (metaAfter || this.metadataSnapshot()) : null;
    const metaChanged = !!beforeMeta &&
      JSON.stringify(beforeMeta) !== JSON.stringify(afterMeta);
    if (!changes.length && !metaChanged) return false;
    this.history.undo.push({
      changes,
      metaBefore: metaChanged ? beforeMeta : null,
      metaAfter: metaChanged ? afterMeta : null,
    });
    if (this.history.undo.length > HISTORY_LIMIT) this.history.undo.shift();
    this.history.redo.length = 0;
    return true;
  }

  undo() {
    if (this.history.active) this.commitTransaction();
    const operation = this.history.undo.pop();
    if (!operation) return false;
    this.applyMetadata(operation.metaBefore);
    for (const [ix, iz, before] of operation.changes) {
      this.setCell(ix, iz, before, { record: false });
    }
    this.recomputeDerived();
    this.history.redo.push(operation);
    return true;
  }

  redo() {
    const operation = this.history.redo.pop();
    if (!operation) return false;
    this.applyMetadata(operation.metaAfter);
    for (const [ix, iz, , after] of operation.changes) {
      this.setCell(ix, iz, after, { record: false });
    }
    this.recomputeDerived();
    this.history.undo.push(operation);
    return true;
  }

  clearHistory() {
    this.history.undo.length = 0;
    this.history.redo.length = 0;
    this.history.active = null;
  }

  clearDirtyChunks() {
    this.dirtyChunks.clear();
  }

  consumeDirtyChunks() {
    const dirty = [...this.dirtyChunks];
    this.dirtyChunks.clear();
    return dirty;
  }

  rebase(dx, dz) {
    const next = new GridDocument({
      origin: this.origin,
      shape: this.shape,
      cellSize: this.cellSize,
    });
    this.forEachCell((ix, iz, value) => {
      next.setCell(ix - dx, iz - dz, value, { record: false });
    });
    this.chunks = next.chunks;
    this.dirtyChunks = next.dirtyChunks;
    this.freeCount = next.freeCount;
    this.blockedCount = next.blockedCount;
    this.bbox = next.bbox;
    this.derivedDirty = false;
    this.revision++;
  }
}
