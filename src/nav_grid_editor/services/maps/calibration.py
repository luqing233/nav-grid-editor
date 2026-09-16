# -*- coding: utf-8 -*-
"""纯 Python 仿射标定与 RANSAC 拟合。"""

from __future__ import annotations


def _mat_vec(m2x3, x, y):
    """2x3 仿射矩阵 [x, y, 1] 相乘"""
    return (m2x3[0][0] * x + m2x3[0][1] * y + m2x3[0][2],
            m2x3[1][0] * x + m2x3[1][1] * y + m2x3[1][2])


def _gauss_solve(a, b):
    """高斯消元（部分主元）解线性方程组 a x = b"""
    n = len(b)
    m = [a[i][:] + [b[i]] for i in range(n)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            raise ValueError("奇异矩阵：控制点可能共线/重合")
        m[col], m[piv] = m[piv], m[col]
        pv = m[col][col]
        for j in range(col, n + 1):
            m[col][j] /= pv
        for r in range(n):
            if r == col:
                continue
            f = m[r][col]
            if abs(f) < 1e-15:
                continue
            for j in range(col, n + 1):
                m[r][j] -= f * m[col][j]
    return [m[i][n] for i in range(n)]


def _solve_affine(pixel, world):
    """最小二乘解 像素→世界 的 2x3 仿射矩阵（正规方程 + 高斯消元）"""
    n = len(pixel)
    if n < 3:
        raise ValueError("至少需要 3 个控制点")
    ata = [[0.0] * 6 for _ in range(6)]
    atb = [0.0] * 6
    for (px, py), (wx, wy) in zip(pixel, world):
        r1 = [px, py, 1.0, 0.0, 0.0, 0.0]
        for j in range(6):
            atb[j] += r1[j] * wx
            for k in range(6):
                ata[j][k] += r1[j] * r1[k]
        r2 = [0.0, 0.0, 0.0, px, py, 1.0]
        for j in range(6):
            atb[j] += r2[j] * wy
            for k in range(6):
                ata[j][k] += r2[j] * r2[k]
    s = _gauss_solve(ata, atb)
    return [[s[0], s[1], s[2]], [s[3], s[4], s[5]]]


def _invert_affine(m):
    """2x3 仿射矩阵解析求逆（世界→像素）"""
    a, b, c = m[0]
    d, e, f = m[1]
    det = a * e - b * d
    if abs(det) < 1e-12:
        raise ValueError("矩阵不可逆（退化仿射）")
    return [[e / det, -b / det, (b * f - e * c) / det],
            [-d / det, a / det, (d * c - a * f) / det]]


def _fit_affine_ransac(pixel, world, threshold=5.0, iterations=120, seed=7):
    """RANSAC 拟合：随机 3 点抽样，取内点最多的模型，内点最小二乘重估。

    返回 (matrix, inlier_flags)，inlier_flags 与 pixel 等长。
    """
    import random
    rng = random.Random(seed)
    n = len(pixel)
    best_inliers = []
    for _ in range(iterations):
        idx = rng.sample(range(n), 3)
        try:
            m = _solve_affine([pixel[i] for i in idx], [world[i] for i in idx])
        except ValueError:
            continue
        inl = []
        for i in range(n):
            wx, wz = _mat_vec(m, pixel[i][0], pixel[i][1])
            d = ((wx - world[i][0]) ** 2 + (wz - world[i][1]) ** 2) ** 0.5
            if d <= threshold:
                inl.append(i)
        if len(inl) > len(best_inliers):
            best_inliers = inl
    if not best_inliers:
        best_inliers = list(range(n))  # 全军覆没时退化为全点最小二乘
    matrix = _solve_affine([pixel[i] for i in best_inliers],
                           [world[i] for i in best_inliers])
    flags = [i in best_inliers for i in range(n)]
    return matrix, flags
