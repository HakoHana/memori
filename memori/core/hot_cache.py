"""热消息缓存 — 内存 deque，每用户最近 20 条消息，免查 DB"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

from ..utils.context_formatter import format_msg
from .interfaces import IHotMessageCache


class HotMessageCache(IHotMessageCache):
    """每个用户的热消息缓存

    在 memory_core.on_message 入口处写入，
    Retriever.get_recent_context 优先从此读取。

    格式与 ConversationStore 兼容，但完全是内存操作。
    """

    MAX_PER_USER = 20

    def __init__(self):
        self._caches: dict[str, deque[dict[str, Any]]] = {}

    # ── 写入 ──

    def push(
        self,
        user_id: str,
        role: str,
        content: str,
        sender_name: str = "",
        sender_id: str = "",
    ):
        """追加一条消息到用户的热缓存"""
        if not user_id:
            return
        if user_id not in self._caches:
            self._caches[user_id] = deque(maxlen=self.MAX_PER_USER)
        self._caches[user_id].append({
            "role": role,
            "content": content,
            "sender_name": sender_name,
            "sender_id": sender_id or user_id,
            "timestamp": time.time(),
        })

    # ── 读取 ──

    def get_recent(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """取最近 N 条原始消息（最新的在末尾）"""
        q = self._caches.get(user_id)
        if not q:
            return []
        messages = list(q)
        return messages[-limit:]

    def format_recent_context(
        self, user_id: str, limit: int = 20, bot_name: str = "我"
    ) -> str:
        """格式化为带时间戳的对话文本"""
        messages = self.get_recent(user_id, limit)
        if not messages:
            return ""
        now = time.time()
        lines = []
        for m in messages:
            ts = m.get("timestamp", now)
            content = m["content"]
            role = m["role"]
            name = m.get("sender_name", "")
            sid = m.get("sender_id", "")
            if role == "user":
                display = name if name else (sid or "用户")
            else:
                display = f"Bot: {bot_name}"
            lines.append(format_msg(ts, display, content, now))
        return "\n".join(lines)

    # ── 管理 ──

    def clear(self, user_id: str | None = None):
        """清空指定用户或全部缓存"""
        if user_id:
            self._caches.pop(user_id, None)
        else:
            self._caches.clear()

    def stats(self) -> dict[str, int]:
        """返回每用户的消息数（调试用）"""
        return {uid: len(q) for uid, q in self._caches.items()}
