"""会话存储 — 保存对话历史，为日记提供完整上下文"""

from __future__ import annotations

import json
import time

from .base_store import BaseDbStore


class ConversationStore(BaseDbStore):
    """存储用户和 Bot 的对话消息"""

    async def initialize(self):
        async with self._connect() as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    platform TEXT DEFAULT '',
                    created_at REAL NOT NULL,
                    last_active_at REAL NOT NULL,
                    message_count INTEGER DEFAULT 0,
                    metadata TEXT DEFAULT '{}'
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    metadata TEXT DEFAULT '{}'
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_session
                ON messages(session_id, id DESC)
            """)
            await db.commit()

    async def add_message(self, session_id: str, user_id: str, role: str, content: str):
        """添加一条消息"""
        now = time.time()
        async with self._connect() as db:
            await db.execute(
                "INSERT OR IGNORE INTO sessions(session_id, user_id, created_at, last_active_at) VALUES (?,?,?,?)",
                (session_id, user_id, now, now),
            )
            await db.execute(
                "UPDATE sessions SET last_active_at=?, message_count=message_count+1 WHERE session_id=?",
                (now, session_id),
            )
            await db.execute(
                "INSERT INTO messages(session_id, role, content, timestamp) VALUES (?,?,?,?)",
                (session_id, role, content, now),
            )
            await db.commit()

    async def get_recent_context(self, session_id: str, limit: int = 20,
                                  user_name: str = "", bot_name: str = "我") -> str:
        """获取最近的对话上下文（用于写日记/判断）

        Args:
            user_name: 用户称呼（Hako/渋夜旅等），空则显示 user_id
            bot_name: Bot 自称，默认"我"
        """
        async with self._connect() as db:
            rows = await db.execute_fetchall(
                "SELECT role, content FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            )
        if not rows:
            return ""
        lines = []
        for r in reversed(rows):
            if r[0] == "user":
                lines.append(f"{user_name or '用户'}: {r[1]}")
            else:
                lines.append(f"{bot_name}: {r[1]}")
        return "\n".join(lines)

    async def get_session_id(self, event) -> str:
        """从事件中提取会话 ID"""
        if hasattr(event, "unified_msg_origin"):
            sid = event.unified_msg_origin
            if sid:
                return str(sid)
        if hasattr(event, "get_session_id"):
            return event.get_session_id() or "default"
        return "default"

    async def get_user_id(self, event) -> str:
        """从事件中提取用户 ID"""
        if hasattr(event, "get_sender_id"):
            sid = event.get_sender_id()
            if sid:
                return str(sid)
        return "Hana"
