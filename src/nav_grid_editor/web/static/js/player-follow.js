export function playerMapPixel(pos, mapping) {
  if (!pos || !mapping || !Array.isArray(mapping.inverse_matrix)) return null;
  const x = Number(pos.x);
  const z = Number(pos.z);
  if (!Number.isFinite(x) || !Number.isFinite(z)) return null;
  const inv = mapping.inverse_matrix;
  const px = Number(inv[0]?.[0]) * x + Number(inv[0]?.[1]) * z + Number(inv[0]?.[2]);
  const py = Number(inv[1]?.[0]) * x + Number(inv[1]?.[1]) * z + Number(inv[1]?.[2]);
  return Number.isFinite(px) && Number.isFinite(py) ? [px, py] : null;
}

export function centeredPlayerPan(point, viewportWidth, viewportHeight, scale) {
  const px = Number(point?.[0]);
  const py = Number(point?.[1]);
  const width = Number(viewportWidth);
  const height = Number(viewportHeight);
  const zoom = Number(scale);
  if (![px, py, width, height, zoom].every(Number.isFinite) ||
      width <= 0 || height <= 0 || zoom <= 0) {
    return null;
  }
  return {
    x: width / 2 - px * zoom,
    y: height / 2 - py * zoom,
  };
}

export function playerPositionMoved(previous, next, epsilon = 1e-6) {
  if (!previous || !next) return !!next;
  const px = Number(previous.x);
  const pz = Number(previous.z);
  const nx = Number(next.x);
  const nz = Number(next.z);
  if (![px, pz, nx, nz].every(Number.isFinite)) return true;
  return Math.abs(nx - px) > epsilon || Math.abs(nz - pz) > epsilon;
}
