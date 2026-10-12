export const RENDER_LAYERS = Object.freeze({
  BASE: 1,
  GRID: 2,
  SELECTION: 4,
  PATH: 8,
  LINK: 16,
  ALL: 31,
});

export class RenderScheduler {
  constructor(renderFrame) {
    this.renderFrame = renderFrame;
    this.pendingLayers = 0;
    this.queued = false;
    this.frame = 0;
  }

  request(layers = RENDER_LAYERS.ALL) {
    this.pendingLayers |= layers;
    if (this.queued) return;
    this.queued = true;
    this.frame = requestAnimationFrame(() => {
      this.frame = 0;
      this.queued = false;
      const layersToRender = this.pendingLayers;
      this.pendingLayers = 0;
      this.renderFrame(layersToRender);
    });
  }

  cancel() {
    if (this.frame) cancelAnimationFrame(this.frame);
    this.frame = 0;
    this.queued = false;
    this.pendingLayers = 0;
  }
}
