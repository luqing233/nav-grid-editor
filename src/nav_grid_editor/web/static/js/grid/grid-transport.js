import { GridDocument } from "./grid-document.js";

export async function loadGridDocument(map, zoom) {
  const response = await fetch(
    "/api/grid2d?map=" + encodeURIComponent(map) + "&zoom=" + encodeURIComponent(zoom),
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
      cells: result.data.cells || [],
      blocked: result.data.blocked || [],
    }),
  };
}

export async function saveGridDocument(map, zoom, document) {
  const sparse = document.exportSparse();
  const response = await fetch("/api/grid2d", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      map,
      zoom,
      data: {
        origin: [document.origin[0], 0, document.origin[1]],
        shape: document.shape,
        cell_size: document.cellSize,
        cells: sparse.cells,
        blocked: sparse.blocked,
      },
    }),
  });
  return response.json();
}
