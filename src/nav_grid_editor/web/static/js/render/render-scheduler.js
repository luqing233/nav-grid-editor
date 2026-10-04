export const RENDER_LAYERS = Object.freeze({
  BASE: 1,
  GRID: 2,
  SELECTION: 4,
  PATH: 8,
  ALL: 15,
});

export class RenderScheduler {
  constructor(renderFrame) {
    this.renderFrame = renderFrame;
    this.pendingLayers = 0;
    this.queued = false;
  }

  request(layers = RENDER_LAYERS.ALL) {
    this.pendingLayers |= layers;
    if (this.queued) return;
    this.queued = true;
    requestAnimationFrame(() => {
      this.queued = false;
      const layersToRender = this.pendingLayers;
      this.pendingLayers = 0;
      this.renderFrame(layersToRender);
    });
  }
}
