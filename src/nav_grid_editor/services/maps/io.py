# -*- coding: utf-8 -*-
"""文件原子写入、无 Pillow PNG 生成和缩略图编码。"""

from __future__ import annotations

import math
import os
import struct
import tempfile
import threading
import time
import zlib
from pathlib import Path

from .settings import Image, WEBP_OK


def solid_png(w: int, h: int, rgb: tuple) -> bytes:
    def chunk(typ: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + typ + data
            + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)
        )
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8bit RGB
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))  # 每行 filter=0
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def _log(msg: str):
    print(f"[map] {msg}", flush=True)


def _unique_tmp(path: Path) -> Path:
    """在 path 同目录下开一个**唯一**的临时文件名，供"写完再原子替换"用。

    以前各处都用固定名 ``<name>.tmp``：两个并发请求（双击保存、两个标签页
    看同一张图）会抢同一个临时文件——先完成的那个 os.replace 之后临时文件
    就没了，另一个的 os.replace 直接 FileNotFoundError；更糟的时序是自检
    读到对方写了一半的内容，把一个残缺文件替换成正式产物。
    同一个目录是必须的，跨盘/跨分区 os.replace 不是原子的。
    """
    fd, name = tempfile.mkstemp(dir=str(path.parent),
                                prefix=path.name + ".", suffix=".tmp")
    os.close(fd)
    return Path(name)


def _as_int(v) -> int:
    """尽量把请求体里的值转成 int，转不动就当 0。

    image_size 直接来自 JSON，可能是 "abc" / "12.5" / NaN / Infinity / 对象；
    int() 对它们分别抛 ValueError / OverflowError / TypeError，而这里没有异常
    边界，漏出去就是一个没有 JSON 响应的断连。
    """
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return 0
    return int(f) if math.isfinite(f) else 0


_write_locks: dict[str, threading.Lock] = {}
_write_locks_guard = threading.Lock()


def _path_lock(path: Path) -> threading.Lock:
    """按目标路径取一把进程内的锁，让同一个文件的写入串行化。

    只有唯一临时名还不够：Windows 上两个线程同时 os.replace 到同一个目标时，
    其中一个会拿到 WinError 5「拒绝访问」（目标正被另一个替换操作占用）。
    实测 12 个并发保存会有 5 个失败，加锁后全部成功。
    """
    key = os.path.normcase(os.path.abspath(str(path)))
    with _write_locks_guard:
        lock = _write_locks.get(key)
        if lock is None:
            lock = _write_locks[key] = threading.Lock()
        return lock


def _replace_with_retry(tmp: Path, dest: Path, attempts: int = 12) -> None:
    """os.replace(tmp, dest)，遇到"被占用"就退避重试。

    写者之间的竞争由 :func:`_path_lock` 解决；读者（并发的 get_grid2d /
    下载总图 / 缩略图生成）打开着 dest 时仍会让替换失败——Windows 要求替换
    目标有独占的删除权限，而 Python 的 open()/np.load() 不共享删除。这属于
    瞬时冲突，退避重试即可（累计等待上限约 2 秒）。
    """
    for i in range(attempts):
        try:
            os.replace(tmp, dest)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(min(0.05 * (i + 1), 0.2))


#: 缩略总图的长边上限与 WebP 质量
OVERVIEW_MAX_PX = 2048
OVERVIEW_QUALITY = 82
#: 没有 WebP 支持时退回 PNG（文件名后缀要跟着变，路由按后缀给 Content-Type）
OVERVIEW_SUFFIX = ".overview.webp" if WEBP_OK else ".overview.png"


def _save_overview(img, out: Path) -> tuple[int, int]:
    """把 img 缩成缩略图写到 out，返回缩略图尺寸。

    **就地缩小 img**：`thumbnail()` 是原地修改的，调用方在这之后不能再拿原图
    当完整尺寸用。两个调用点（`_compose_run` 存完总图后、`ensure_overview`）
    都满足这个前提——复制一张 8192x7680 的 RGB 图要多花约 190MB 内存，不值。

    编码用 WebP：同一张 2048 长边的缩略图，PNG 是 2.3MB、WebP q82 只有 195KB
    （小 12 倍），编码还更快；浏览器要解码的像素数一样，但下载量和传输开销都
    降下来了。注意别加 `optimize=True`——实测多花 5 倍编码时间只换来 4% 体积。
    """
    img.thumbnail((OVERVIEW_MAX_PX, OVERVIEW_MAX_PX), Image.Resampling.BILINEAR)
    tmp = _unique_tmp(out)
    with _path_lock(out):
        try:
            if WEBP_OK:
                img.save(tmp, "WEBP", quality=OVERVIEW_QUALITY, method=4)
            else:  # pragma: no cover
                img.save(tmp, "PNG")
            _replace_with_retry(tmp, out)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
    return img.size


# =========================================================
# 事件总线（SSE 广播）
# =========================================================
