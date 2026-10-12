import { GridDocument } from "./grid-document.js";

export async function loadGridDocument(map, zoom, tier = "") {
  const response = await fetch(
    "/api/grid2d?map=" + encodeURIComponent(map) +
    "&zoom=" + encodeURIComponent(zoom) +
    "&tier=" + encodeURIComponent(tier),
  );
  const result = await response.json();
  if (!result.data) return { document: null, response: result };
  const origin = result.data.origin || [0, 0, 0];
  return {
    response: result,
    document: GridDocument.fromSparse({
      origin: [origin[0], origin[2]],
      shape: result.data.shape
        ? [result.data.shape[0], result.data.shape[1]]
        : null,
      cellSize: result.data.cell_size || 1,
      floorRegions: result.data.floor_regions || [],
      sourceTier: result.data.tier || "",
      cells: result.data.cells || [],
      blocked: result.data.blocked || [],
    }),
  };
}

export async function saveGridDocument(map, zoom, tier, document) {
  const sparse = document.exportSparse();
  const response = await fetch("/api/grid2d", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      map,
      zoom,
      tier,
      data: {
        origin: [document.origin[0], 0, document.origin[1]],
        shape: document.shape,
        cell_size: document.cellSize,
        floor_regions: document.floorRegions || [],
        source_tier: document.sourceTier || "",
        cells: sparse.cells,
        blocked: sparse.blocked,
      },
    }),
  });
  return response.json();
}
