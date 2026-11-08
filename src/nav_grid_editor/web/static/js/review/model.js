import { GridDocument } from "../grid/grid-document.js";

export const TRANSITIONS = Object.freeze({
  "0>1": {label:"未知 → 可走", color:[115,245,70,210]},
  "1>0": {label:"可走 → 未知", color:[255,168,45,220]},
  "2>1": {label:"阻挡 → 可走", color:[255,65,85,240]},
  "1>2": {label:"可走 → 阻挡", color:[185,110,255,230]},
  "0>2": {label:"未知 → 阻挡", color:[255,110,90,210]},
  "2>0": {label:"阻挡 → 未知", color:[60,200,255,210]},
});
export const STATE_NAMES = ["未知", "可走", "阻挡"];

export function regionsOf(report, mapLabel = value => value) {
  return report.files.flatMap((file, fileIndex) => file.regions.map((region, regionIndex) => ({
    ...region, fileIndex, regionIndex, label:`${mapLabel(file.map)} · ${file.tier || "主图"} · ${region.metadata ? "元数据" : region.count + " 格"}`,
  })));
}

export function snapshotDocument(file, side) {
  return file[side] ? GridDocument.fromSparse(file[side]) : null;
}

export function transitionAt(file, x, z, lookup) {
  const ix = Math.floor((x - file.origin[0]) / file.cellSize);
  const iz = Math.floor((z - file.origin[1]) / file.cellSize);
  return lookup.get(ix + "," + iz) || null;
}
