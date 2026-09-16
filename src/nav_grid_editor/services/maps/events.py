# -*- coding: utf-8 -*-
"""线程安全的 SSE 事件总线。"""

from __future__ import annotations

import json
import queue
import threading


class EventBus:
    def __init__(self):
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=4000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            try:
                self._subs.remove(q)
            except ValueError:
                pass

    def emit_obj(self, event: dict):
        """广播一个事件（自动 JSON 化）"""
        text = json.dumps(event, ensure_ascii=False)
        with self._lock:
            for q in self._subs:
                try:
                    q.put_nowait(text)
                except queue.Full:
                    try:  # 慢客户端：丢最旧的一条
                        q.get_nowait()
                        q.put_nowait(text)
                    except Exception:
                        pass

    def emit(self, **kw):
        self.emit_obj(kw)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)


# =========================================================
# 瓦片存储（读取 / 解析，与 wsserver 目录结构兼容）
# =========================================================
