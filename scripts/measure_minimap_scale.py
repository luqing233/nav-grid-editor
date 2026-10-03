# -*- coding: utf-8 -*-
"""由游戏截图实测小地图比例尺（米/像素）。

游戏小地图是**北向上、以玩家箭头为中心**的世界地图贴图（不随视角旋转，见下）。
把截图里那一小块圆形贴图拿去和已标定的拼接总图做模板匹配，就能直接量出
「小地图 1 像素 = 多少米」。

原理
----
总图已标定（``<地图>_<zoom>_mapping.json``），zoom 4 的总图正好是 4 像素/米。
给定一个候选比例尺 ``mpp``（米/小地图像素），把总图按 ``0.25/mpp`` 降采样成
「1 像素 = 1 小地图像素」的参考图，再在玩家位置附近滑窗做归一化互相关（NCC）。
扫描 ``mpp``，NCC 峰值处的 ``mpp`` 就是答案。**不做角度搜索**——地图是北向上的，
这一点可以先在两张截图之间用纯平移 NCC 验证（实测同位置两张截图给 0.97）。

两种用法
--------
单张（自己找圆盘，启发式，左上角 UI 控件可能干扰）::

    uv run python scripts/measure_minimap_scale.py shot.png --world -1293 -607

批处理（推荐；配 ok-end-field 的「小地图比例尺采集」任务产出的数据用）::

    uv run python scripts/measure_minimap_scale.py --batch logs/minimap_scale_capture --emit-config

``--batch`` 三种入参都认：

- ``.../index.json`` —— 整份索引；
- **目录** —— 有 ``index.json`` 就用它，没有就把目录下所有 ``*.json`` 当逐图 sidecar 收
  （只搬了几张图、没带索引也能跑）；
- **单张图片** —— 自动找同目录同主名的 ``.json`` sidecar。

批处理直接读采集时记下的**精确 WS 坐标**和 ``region_geometry`` 给出的圆心半径，不猜
圆盘位置，所以比单张模式可靠得多。

实测（map02，玩家在 (-1293, -607) 附近，四张不同分辨率的截图）
------------------------------------------------------------------

    截图宽 W(px)    mpp(米/像素)     mpp × W
    2000            0.8535           1707
    2000            0.8535           1707
    1924            0.8905           1713
    1924            0.8885           1710

``mpp × W`` 稳定在 ``1709 ± 3``（离散度 0.37%）。圆盘半径 ``R/W`` 稳定在 ≈0.0452，
于是小地图覆盖的世界半径 ≈ ``1709 × 0.0452 ≈ 77`` 米（直径 ≈154 米）。

把假设坐标在 X/Z 上各扰动 ±40 米（合位移 57 米）重跑，``mpp`` 只在 0.882~0.896
之间变化（±0.8%）——位置误差被滑窗搜索吸收，所以本方法**不要求坐标很准**。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

import numpy as np
from PIL import Image

# 总图很大（map02 zoom4 是 10752x15872），关掉 PIL 的解压炸弹阈值。
Image.MAX_IMAGE_PIXELS = None

#: 小地图在画面上的默认搜索区域（占全画面的比例）：左上角。
DEFAULT_ROI = (0.0, 0.0, 0.22, 0.40)
#: 小地图圆盘半径相对画面宽度的实测比例（见模块文档）。
DEFAULT_R_RATIO = 0.0452
#: 候选圆心用到的半径搜索带（占画面宽度），收窄到实测的小地图半径附近。
RADIUS_BAND = (0.042, 0.050)
#: 默认扫描的米/像素范围。
DEFAULT_RANGE = (0.40, 1.40)
#: 参与地图匹配验证的候选圆心个数。
N_CANDIDATES = 6
#: 掩膜半径相对圆盘半径的收缩比（避开边缘描边）。
MASK_SHRINK = 0.92


def load_world_to_pixel(data_root: Path, map_name: str, zoom: str) -> np.ndarray:
    """读标定文件，返回世界 (x, z) -> 总图像素 (px, py) 的 2x3 仿射矩阵。

    标定文件里 ``matrix`` 是像素->世界、``inverse_matrix`` 才是世界->像素，
    这里取后者。
    """
    path = data_root / "assets/tiles/maps" / map_name / zoom / f"{map_name}_{zoom}_mapping.json"
    if not path.is_file():
        legacy = data_root / "assets/tiles/maps" / f"{map_name}_{zoom}_mapping.json"
        if legacy.is_file():
            path = legacy
        else:
            raise SystemExit(f"找不到标定文件: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    inv = data.get("inverse_matrix")
    if not inv or len(inv) != 2 or any(len(r) != 3 for r in inv):
        raise SystemExit(f"标定文件缺少合法的 inverse_matrix: {path}")
    return np.asarray(inv, dtype=np.float64)


# ---------------------------------------------------------------------- #
# 圆盘定位（单张模式的启发式；批处理模式不用它）
# ---------------------------------------------------------------------- #
def _ring_profile(mag: np.ndarray, cx: float, cy: float, r_hi: float, rb: int, nang: int) -> np.ndarray:
    """以 (cx,cy) 为圆心，把梯度幅值按 (角度扇区, 半径) 分箱，返回每半径的扇区中位数。

    取**中位数而不是平均**是关键：任何一条直边都会在某个半径上拉高「环平均」，
    但只覆盖少数扇区；真实圆盘边缘在所有扇区上都有响应，中位数能保住它。
    """
    h, w = mag.shape
    x0 = max(0, int(cx - r_hi - 2))
    y0 = max(0, int(cy - r_hi - 2))
    x1 = min(w, int(cx + r_hi + 3))
    y1 = min(h, int(cy + r_hi + 3))
    ring = np.full(rb, np.nan)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return ring
    ys, xs = np.mgrid[y0:y1, x0:x1]
    dx = xs - cx
    dy = ys - cy
    r = np.hypot(dx, dy)
    sel = r < r_hi
    sub = mag[y0:y1, x0:x1][sel]
    ang = (np.arctan2(dy[sel], dx[sel]) + np.pi) / (2 * np.pi)
    ab = np.minimum((ang * nang).astype(np.int32), nang - 1)
    bi = r[sel].astype(np.int32)
    idx = ab * rb + bi
    acc = np.bincount(idx, weights=sub, minlength=nang * rb).reshape(nang, rb)
    cnt = np.bincount(idx, minlength=nang * rb).reshape(nang, rb)
    with np.errstate(invalid="ignore"):
        prof = np.where(cnt > 0, acc / np.maximum(cnt, 1), np.nan)
        coverage = (cnt > 0).sum(axis=0) >= nang * 0.5
        return np.where(coverage, np.nanmedian(prof, axis=0), np.nan)


def find_minimap_candidates(
    img: Image.Image, roi: tuple[float, float, float, float], limit: int = N_CANDIDATES
) -> list[tuple[float, float, float, float]]:
    """在 ROI 内找出若干小地图候选圆盘，返回 [(cx, cy, radius, 圆度得分)]。

    只判断「像不像圆」，不判断真假——真假交给调用方的地图匹配。UI 上的圆角控件
    （「探索」药丸之类）在这一步得分很高，所以单张模式必须靠 NCC 复选。
    """
    gray = np.asarray(img.convert("L")).astype(np.float64)
    gy, gx = np.gradient(gray)
    mag = np.hypot(gx, gy)
    w, h = img.size
    r_lo, r_hi = RADIUS_BAND[0] * w, RADIUS_BAND[1] * w
    rb = int(r_hi) + 2
    nang = 72

    def score(cx: float, cy: float) -> tuple[float, float]:
        ring = _ring_profile(mag, cx, cy, r_hi, rb, nang)
        band = ring[int(r_lo):int(r_hi)]
        if not np.any(np.isfinite(band)):
            return -1.0, 0.0
        k = int(np.nanargmax(band))
        i = k + int(r_lo)
        # 「峰高 - 两侧 5px 均值」：奖励窄峰，压掉宽平台。
        sharp = float(band[k] - 0.5 * (np.nan_to_num(ring[max(0, i - 5)])
                                       + np.nan_to_num(ring[min(rb - 1, i + 5)])))
        return sharp, float(i)

    x0, y0, x1, y1 = int(roi[0] * w), int(roi[1] * h), int(roi[2] * w), int(roi[3] * h)
    step = max(4.0, r_lo / 5)
    rough: list[tuple[float, float, float, float]] = []
    for cy in np.arange(y0 + r_lo * 0.5, y1 - r_lo * 0.5 + 1e-9, step):
        for cx in np.arange(x0 + r_lo * 0.5, x1 - r_lo * 0.5 + 1e-9, step):
            s, r = score(cx, cy)
            rough.append((s, float(cx), float(cy), r))
    if not rough:
        return []
    rough.sort(key=lambda t: -t[0])

    # 非极大值抑制：候选之间至少隔开一个半径，避免全挤在同一个 UI 控件上。
    kept: list[tuple[float, float, float]] = []
    for s, cx, cy, _ in rough:
        if all(np.hypot(cx - kx, cy - ky) > r_lo for kx, ky in kept):
            kept.append((cx, cy))
        if len(kept) >= limit:
            break

    out = []
    for cx0, cy0 in kept:
        best = None
        for cy in np.arange(cy0 - step, cy0 + step + 1e-9, 0.5):
            for cx in np.arange(cx0 - step, cx0 + step + 1e-9, 0.5):
                s, r = score(cx, cy)
                if best is None or s > best[0]:
                    best = (s, cx, cy, r)
        out.append(best)
    out.sort(key=lambda t: -t[0])
    return out


# ---------------------------------------------------------------------- #
# 比例尺扫描
# ---------------------------------------------------------------------- #
def correlate_valid(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """``out[i,j] = Σ_{u,v} a[i+u, j+v] · b[u,v]``（FFT 实现的互相关）。"""
    a0, a1 = a.shape
    b0, b1 = b.shape
    shape = (a0 + b0 - 1, a1 + b1 - 1)
    full = np.fft.irfft2(np.fft.rfft2(a, shape) * np.conj(np.fft.rfft2(b, shape)), s=shape)
    return full[: a0 - b0 + 1, : a1 - b1 + 1]


def masked_ncc(window: np.ndarray, template: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """带圆形掩膜的归一化互相关图。"""
    n = float(mask.sum())
    t = template - (template * mask).sum() / n
    t = t * mask
    t_sq = float((t * t).sum())
    num = correlate_valid(window, t)
    s1 = correlate_valid(window, mask)
    s2 = correlate_valid(window * window, mask)
    var = np.maximum(s2 - s1 * s1 / n, 1e-9)
    return num / np.sqrt(t_sq * var)


class Reference:
    """从总图上取参考图（1 像素 = 1 小地图像素）。

    总图很大，PIL 每次 ``crop`` 都要整张解码一遍，所以这里**只在初始化时裁剪一次**
    （取扫描范围上限所需的最大范围），之后各比例尺都从这块内存里的图再裁。
    """

    def __init__(self, composite: Image.Image, px: float, py: float, window_size: int, mpp_hi: float):
        need = int(round(window_size * mpp_hi * 4.0)) + 2  # 总图是 4 像素/米
        h = need // 2
        left = int(round(px)) - h
        top = int(round(py)) - h
        if left < 0 or top < 0 or left + 2 * h > composite.width or top + 2 * h > composite.height:
            self.base = None
            return
        self.base = composite.crop((left, top, left + 2 * h, top + 2 * h)).convert("L")
        self.center = (int(round(px)) - left, int(round(py)) - top)
        self.window_size = window_size

    def at(self, mpp: float) -> np.ndarray | None:
        if self.base is None:
            return None
        need = int(round(self.window_size * mpp * 4.0))
        h = need // 2
        cx, cy = self.center
        left, top = cx - h, cy - h
        if left < 0 or top < 0 or left + 2 * h > self.base.width or top + 2 * h > self.base.height:
            return None
        region = self.base.crop((left, top, left + 2 * h, top + 2 * h))
        return np.asarray(region.resize((self.window_size, self.window_size), Image.BILINEAR)).astype(np.float64)


def scan_mpp(ref: Reference, template: np.ndarray, mask: np.ndarray, margin: int,
             lo: float, hi: float, steps: int) -> tuple[float, float, tuple[int, int]] | None:
    """扫 ``mpp``，返回 (NCC, 亚像素 mpp, 相对窗口中心的对齐残差 px)。"""
    profile: dict[float, float] = {}
    best = None
    for mpp in np.arange(lo, hi + 1e-9, (hi - lo) / max(steps, 1)):
        window = ref.at(float(mpp))
        if window is None:
            continue
        ncc = masked_ncc(window, template, mask)
        k = int(ncc.argmax())
        iy, ix = np.unravel_index(k, ncc.shape)
        val = float(ncc.ravel()[k])
        profile[round(float(mpp), 5)] = val
        if best is None or val > best[0]:
            best = (val, float(mpp), (int(ix) - margin, int(iy) - margin))
    if best is None:
        return None
    # 抛物线亚像素细化
    keys = sorted(profile)
    mpp = best[1]
    idx = keys.index(round(best[1], 5))
    if 0 < idx < len(keys) - 1:
        y0_, y1_, y2_ = profile[keys[idx - 1]], profile[keys[idx]], profile[keys[idx + 1]]
        den = y0_ - 2 * y1_ + y2_
        if abs(den) > 1e-12:
            mpp = keys[idx] + 0.5 * (y0_ - y2_) / den * (keys[idx + 1] - keys[idx])
    return best[0], mpp, best[2]


def measure_at(shot: Image.Image, cx: float, cy: float, radius: float, world: tuple[float, float],
               inv: np.ndarray, composite: Image.Image, lo: float, hi: float, steps: int) -> dict:
    """在指定的圆盘位置/半径上量一次 m/px。"""
    px = float(inv[0] @ np.array([world[0], world[1], 1.0]))
    py = float(inv[1] @ np.array([world[0], world[1], 1.0]))
    t_size = int(round(2 * radius))
    margin = max(20, int(round(radius * 0.75)))
    window_size = t_size + 2 * margin
    half = t_size // 2
    left = int(round(cx - half))
    top = int(round(cy - half))
    template = np.asarray(shot.convert("L").crop((left, top, left + t_size, top + t_size))).astype(np.float64)
    yy, xx = np.mgrid[0:t_size, 0:t_size]
    mask = (((yy - half + 0.5) ** 2 + (xx - half + 0.5) ** 2) <= (radius * MASK_SHRINK) ** 2).astype(np.float64)
    ref = Reference(composite, px, py, window_size, hi)
    result = scan_mpp(ref, template, mask, margin, lo, hi, steps)
    if result is None:
        raise SystemExit("扫描范围内没有一个比例尺能落在总图内，请检查 --world / --range")
    ncc, mpp, (dx, dy) = result
    return {
        "world": (float(world[0]), float(world[1])),
        "pixel_on_composite": (px, py),
        "minimap_center": (float(cx), float(cy)),
        "minimap_radius_px": float(radius),
        "ncc": ncc,
        "m_per_px": mpp,
        "world_radius_m": radius * mpp,
        "shift_px": (dx, dy),
        # 残差换算成米：窗口 1 像素 = mpp 米；窗口 y 向下，世界 Z 与之反号。
        "shift_m": (dx * mpp, -dy * mpp),
    }


def _summary(rows: list[dict]) -> str:
    """按画面宽度分组汇总。

    必须分组：``m/px`` 随分辨率变化（≈常数/宽度），把不同宽度的样本混在一起取
    中位数会得到一个两种分辨率都不对应的数，离散度也会被宽度差撑大。
    """
    by_width: dict[int, list[dict]] = {}
    for r in rows:
        by_width.setdefault(int(r["width"]), []).append(r)
    lines = [f"样本数        : {len(rows)}"]
    for width in sorted(by_width):
        group = by_width[width]
        mpps = [r["m_per_px"] for r in group]
        med = statistics.median(mpps)
        spread = (max(mpps) - min(mpps)) / med * 100 if len(mpps) > 1 else 0.0
        radii = [r["world_radius_m"] for r in group]
        shifts = [float(np.hypot(*r["shift_px"])) for r in group]
        nccs = [r["ncc"] for r in group]
        lines += [
            "",
            f"  画面宽 {width}px（{len(group)} 条）",
            f"    m/px 中位数   : {med:.4f} 米/像素   离散 {spread:.2f}%"
            f"（{min(mpps):.4f} ~ {max(mpps):.4f}）",
            f"    m/px × 宽度   : {med * width:.0f}   （经验常数 ≈1709，见模块文档）",
            f"    小地图世界半径: {statistics.median(radii):.1f} 米"
            f"（直径 {2 * statistics.median(radii):.1f} 米）",
            f"    NCC           : 中位 {statistics.median(nccs):.4f}，最低 {min(nccs):.4f}",
            f"    对齐残差      : 中位 {statistics.median(shifts):.1f}px，最大 {max(shifts):.1f}px"
            f"  （≈ {statistics.median(shifts) * med:.1f} 米）",
        ]
    return "\n".join(lines)


# ---------------------------------------------------------------------- #
# 两种入口
# ---------------------------------------------------------------------- #
def _config_snippet(rows: list[dict]) -> str:
    """输出可直接粘贴进 ok-end-field ``configs/MinimapPositionTask.json`` 的两个键。

    轴映射用对角形式 ``s,0,0,-s``：小地图纹理各向异性实测 <0.3%（象限配准判据，
    见模块文档），所以非对角项为 0、两轴同一个 s，符号是「世界_x ≈ +s·地图_x，
    世界_z ≈ -s·地图_y」。
    """
    by_width: dict[int, list[dict]] = {}
    for r in rows:
        by_width.setdefault(int(r["width"]), []).append(r)
    out = []
    for width in sorted(by_width):
        s = statistics.median([r["m_per_px"] for r in by_width[width]])
        out.append(
            f"  画面宽 {width}px：\n"
            f'      "比例尺(米/像素)": {s:.3f},\n'
            f'      "轴映射(逗号4值)": "{s:.3f},0,0,-{s:.3f}"'
        )
    const = statistics.median([r["m_per_px"] * r["width"] for r in rows])
    out.append(f"  换算到其它分辨率：s = {const:.0f} / 画面宽度(px)")
    return "\n".join(out)


def load_samples(batch_arg: str) -> tuple[list[dict], Path]:
    """把 ``--batch`` 的入参统一解析成 [(样本记录, ...)] 与基准目录。

    三种入参都认，对应采集侧产出的三种形态：
      - ``.../index.json``：整份索引，样本在 ``samples`` 里（``file`` 相对索引所在目录）；
      - **目录**：有 ``index.json`` 就用它；没有就 glob 目录下所有 ``*.json`` 当逐图
        sidecar 收——这样用户只搬了几张图（没带索引）也能直接跑；
      - **单张图片**：找它同目录同主名的 ``.json`` sidecar。

    读 sidecar 时优先用**紧挨着它的那张图**，而不是记录里的 ``file`` 字段：sidecar 是
    跟着图片走的，``file`` 可能相对保存根目录、搬动后就失效了。
    """
    path = Path(batch_arg)
    if path.is_file() and path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        samples = data.get("samples") if isinstance(data, dict) else data
        if not samples:
            raise SystemExit(f"{path} 里没有样本")
        return samples, path.parent
    if path.is_file():
        sidecar = path.with_suffix(".json")
        if not sidecar.is_file():
            raise SystemExit(f"图片旁边没有同名 sidecar: {sidecar}（单张模式请用 --world）")
        rec = json.loads(sidecar.read_text(encoding="utf-8"))
        if isinstance(rec, dict) and "samples" in rec:
            rec = rec["samples"][0]
        rec = dict(rec)
        rec["file"] = path.name
        return [rec], path.parent
    if path.is_dir():
        index = path / "index.json"
        if index.is_file():
            return load_samples(str(index))
        found = []
        for sidecar in sorted(path.rglob("*.json")):
            if sidecar.name == "index.json":
                continue
            rec = json.loads(sidecar.read_text(encoding="utf-8"))
            if isinstance(rec, dict) and "samples" in rec:
                found.extend(rec["samples"])
                continue
            if not isinstance(rec, dict) or "x" not in rec:
                continue
            img = next((sidecar.with_suffix(e) for e in (".png", ".PNG", ".jpg", ".webp")
                        if sidecar.with_suffix(e).is_file()), None)
            if img is not None:
                rec = dict(rec)
                rec["file"] = str(img.relative_to(path))
            found.append(rec)
        if not found:
            raise SystemExit(f"{path} 下既没有 index.json 也没有可用的 sidecar")
        return found, path
    raise SystemExit(f"找不到 {batch_arg}")


def run_batch(args) -> int:
    """批处理：读 ok-end-field「小地图比例尺采集」产出的 index.json 或逐图 sidecar。"""
    samples, base_dir = load_samples(args.batch)

    data_root = Path(args.data_root).resolve()
    composite_cache: dict[tuple[str, str], Image.Image] = {}
    lo, hi = args.range
    rows, failures = [], []
    for i, s in enumerate(samples, 1):
        map_name = s.get("map_id") or s.get("map") or args.map
        zoom = str(s.get("zoom", args.zoom))
        region = s.get("region") or {}
        if isinstance(region, (list, tuple)):
            cx, cy, radius = float(region[0]), float(region[1]), float(region[3])
        else:
            # region 缺失时退到 UI 比例默认值（ok-end-field 的 DEFAULT_CENTER_RATIO
            # 与实测的 0.0452R/W）。圆心差几十像素不影响结果——滑窗搜索会吸收，
            # 只是会把差值记进对齐残差。
            cx = float(region.get("cx", 0.084 * float(s["width"])))
            cy = float(region.get("cy", 0.154 * float(s["height"])))
            radius = float(region.get("r_outer", DEFAULT_R_RATIO * float(s["width"])))
        if args.center:
            cx, cy = float(args.center[0]), float(args.center[1])
        if args.radius:
            radius = float(args.radius)

        img_path = Path(s["file"])
        if not img_path.is_absolute():
            img_path = base_dir / img_path
        if not img_path.is_file():
            # 索引可能指向已被搬走的图（索引是追加合并的，不会跟着文件消失）。这属于
            # 单条样本的问题，不该让整批跑挂。
            failures.append((i, f"图片不存在: {img_path}"))
            continue
        key = (map_name, zoom)
        if key not in composite_cache:
            p = data_root / "assets/tiles/maps" / map_name / zoom / f"{map_name}_{zoom}.png"
            if not p.is_file():
                failures.append((i, f"找不到拼接总图 {p}"))
                continue
            composite_cache[key] = Image.open(p)

        try:
            shot = Image.open(img_path).convert("RGB")
            inv = load_world_to_pixel(data_root, map_name, zoom)
            r = measure_at(shot, cx, cy, radius, (float(s["x"]), float(s["z"])), inv,
                           composite_cache[key], lo, hi, args.steps)
        except SystemExit as exc:
            failures.append((i, str(exc)))
            continue
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # 单条样本坏掉（图读不出、字段缺失/非有限、坐标超范围…）不该中断整批
            failures.append((i, f"{type(exc).__name__}: {exc}"))
            continue
        r["file"] = str(img_path)
        r["map_id"] = map_name
        r["width"] = int(s.get("width") or shot.size[0])
        r["height"] = int(s.get("height") or shot.size[1])
        rows.append(r)
        print(f"[{i:2d}] {img_path.name}  ({s['x']:.1f}, {s['z']:.1f})  "
              f"m/px={r['m_per_px']:.4f}  NCC={r['ncc']:.4f}  "
              f"残差={r['shift_px']} ({r['shift_m'][0]:+.1f}, {r['shift_m'][1]:+.1f})m")

    if not rows:
        raise SystemExit("所有样本都失败了：\n  " + "\n  ".join(f"{i}: {m}" for i, m in failures))
    print("\n===== 汇总 =====")
    print(_summary(rows))
    if args.emit_config:
        print("\n===== 可直接粘贴进 ok-end-field configs/MinimapPositionTask.json =====")
        print(_config_snippet(rows))
    if failures:
        print(f"\n失败 {len(failures)} 条：")
        for i, m in failures:
            print(f"  [{i}] {m}")
    # 阈值取 0.55 而不是 0.8：真实截图里小地图上会叠 HUD 图标（任务标记、队伍头像），
    # 会把 NCC 压到 0.7 上下，但几何对齐仍然是对的（残差为 0）。真正的失败匹配
    # NCC 只有 0.2~0.4。
    weak = [r for r in rows if r["ncc"] < 0.55]
    if weak:
        print(f"\n注意：{len(weak)} 条 NCC < 0.55，圆盘位置或地图可能不对，逐条看上面的残差。")
    if args.dump:
        Path(args.dump).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n逐条结果已写入 {args.dump}")
    return 0


def run_single(args) -> int:
    data_root = Path(args.data_root).resolve()
    inv = load_world_to_pixel(data_root, args.map, args.zoom)
    shot = Image.open(args.screenshot).convert("RGB")
    width, height = shot.size

    if args.center:
        candidates = [(0.0, float(args.center[0]), float(args.center[1]),
                       float(args.radius) if args.radius else DEFAULT_R_RATIO * width)]
    else:
        candidates = find_minimap_candidates(shot, tuple(args.roi))
        if not candidates:
            raise SystemExit("在小地图搜索区域内没找到圆盘，请用 --center/--radius 指定")
    if args.radius:
        candidates = [(s, cx, cy, float(args.radius)) for s, cx, cy, _ in candidates]

    composite_path = data_root / "assets/tiles/maps" / args.map / args.zoom / f"{args.map}_{args.zoom}.png"
    if not composite_path.is_file():
        raise SystemExit(f"找不到拼接总图: {composite_path}")
    composite = Image.open(composite_path)
    lo, hi = args.range

    winner = None
    for _, cx, cy, radius in candidates:
        try:
            r = measure_at(shot, cx, cy, radius, tuple(args.world), inv, composite, lo, hi, 60)
        except SystemExit:
            continue
        if winner is None or r["ncc"] > winner["ncc"]:
            winner = r
    if winner is None:
        raise SystemExit("所有候选圆盘都匹配失败，请用 --center/--radius 指定圆心半径")
    # 选中的候选再用细步长重跑一次
    winner = measure_at(shot, *winner["minimap_center"], winner["minimap_radius_px"],
                        tuple(args.world), inv, composite, lo, hi, args.steps)

    print(f"截图          : {args.screenshot}  {width}x{height}")
    print(f"世界坐标      : ({winner['world'][0]:.1f}, {winner['world'][1]:.1f})  "
          f"-> 总图像素 ({winner['pixel_on_composite'][0]:.1f}, {winner['pixel_on_composite'][1]:.1f})")
    print(f"小地图圆心    : ({winner['minimap_center'][0]:.1f}, {winner['minimap_center'][1]:.1f})  "
          f"半径 {winner['minimap_radius_px']:.1f}px (R/W={winner['minimap_radius_px'] / width:.5f})")
    print(f"匹配质量 NCC  : {winner['ncc']:.4f}   （对齐残差 {winner['shift_px']} px）")
    print(f"比例尺        : {winner['m_per_px']:.4f} 米/像素")
    print(f"小地图世界半径: {winner['world_radius_m']:.1f} 米（直径 {2 * winner['world_radius_m']:.1f} 米）")
    print(f"m/px × 宽度   : {winner['m_per_px'] * width:.1f}（经验常数 ≈1709，见模块文档）")
    if winner["ncc"] < 0.55:
        print("提示          : NCC 偏低，圆盘可能没定位对；请用 --center/--radius 指定后重跑。",
              file=sys.stderr)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="由游戏截图实测小地图比例尺（米/像素）")
    p.add_argument("screenshot", nargs="?", help="游戏截图路径（单张模式）")
    p.add_argument("--batch", help="批处理：ok-end-field「小地图比例尺采集」的输出目录或 index.json")
    p.add_argument("--world", nargs=2, type=float, metavar=("X", "Z"),
                   help="截图时玩家的世界坐标（单张模式，不需要很准）")
    p.add_argument("--map", default="map02", help="地图名，默认 map02")
    p.add_argument("--zoom", default="4", help="总图层级，默认 4（4 像素/米）")
    p.add_argument("--data-root", default=".", help="数据根目录，默认当前目录")
    p.add_argument("--roi", nargs=4, type=float, default=DEFAULT_ROI,
                   help="小地图搜索区域，占画面比例 x0 y0 x1 y1，默认左上角")
    p.add_argument("--center", nargs=2, type=float, help="直接指定小地图圆心像素坐标")
    p.add_argument("--radius", type=float, help="直接指定小地图圆盘半径（像素）")
    p.add_argument("--range", nargs=2, type=float, default=DEFAULT_RANGE,
                   help="扫描的米/像素范围，默认 0.40 1.40")
    p.add_argument("--steps", type=int, default=200, help="细扫描步数，默认 200")
    p.add_argument("--dump", help="批处理模式：把逐条结果写成 JSON")
    p.add_argument("--emit-config", action="store_true",
                   help="批处理模式：额外输出可粘贴进 ok-end-field MinimapPositionTask.json 的配置片段")
    args = p.parse_args(argv)

    if args.batch:
        return run_batch(args)
    if not args.screenshot:
        p.error("需要给截图路径，或用 --batch")
    if not args.world:
        p.error("单张模式需要 --world X Z")
    return run_single(args)


if __name__ == "__main__":
    sys.exit(main())
