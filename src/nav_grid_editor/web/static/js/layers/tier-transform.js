export function tierToBaseMatrix(layer, inverseMatrix) {
  const width = Number(layer.width);
  const height = Number(layer.height);
  const { west, east, south, north } = layer.bounds || {};
  if (!(width > 0) || !(height > 0)) {
    throw new Error("invalid layer size");
  }
  if (![west, east, south, north].every(Number.isFinite)) {
    throw new Error("invalid layer bounds");
  }
  if (!Array.isArray(inverseMatrix) || inverseMatrix.length !== 2) {
    throw new Error("invalid inverse matrix");
  }

  // Official image bounds are north-west to south-east.  Image v increases
  // while world z decreases, hence the negative vertical scale.
  const sx = (east - west) / width;
  const sz = -(north - south) / height;
  const inv = inverseMatrix;

  return [
    inv[0][0] * sx,
    inv[1][0] * sx,
    inv[0][1] * sz,
    inv[1][1] * sz,
    inv[0][0] * west + inv[0][1] * north + inv[0][2],
    inv[1][0] * west + inv[1][1] * north + inv[1][2],
  ];
}

export function pickLayerGroup(layers, wx, wz) {
  let best = null;
  for (const layer of layers || []) {
    const anchors = Array.isArray(layer.entrances) && layer.entrances.length
      ? layer.entrances
      : layer.entrance ? [layer.entrance] : [];
    for (const anchor of anchors) {
      const dx = Number(wx) - Number(anchor.x);
      const dz = Number(wz) - Number(anchor.z);
      const distance = Math.hypot(dx, dz);
      const radius = Number(anchor.radius || 28);
      if (!Number.isFinite(distance) || !Number.isFinite(radius) || distance > radius) {
        continue;
      }
      if (!best || distance < best.distance) {
        best = {
          group: layer.group,
          groupKey: layer.group_key || layer.group,
          distance,
        };
      }
    }
  }
  if (!best) return null;
  return {
    group: best.group,
    groupKey: best.groupKey,
    layers: (layers || []).filter(
      layer => (layer.group_key || layer.group) === best.groupKey
    ),
  };
}
