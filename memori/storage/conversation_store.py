"""会话存储 — 保存对话历史，为日记提供完整上下文

架构变更（2026-06）：
- 此库从主存储降级为冷备份
- 直读对话历史
- 批量写入 + 保留策略在此实现
"""

from __future__ import annotations

import json
import time

from .base_store import BaseDbStore


class ConversationStore(BaseDbStore):
    """存储用户和 Bot 的对话消息（冷备份角色）"""

    # 保留策略默认值
    DEFAULT_MAX_DAYS = 7
    DEFAULT_MAX_PER_USER = 1000

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
                    sender_id TEXT DEFAULT '',
                    sender_name TEXT DEFAULT '',
                    timestamp REAL NOT NULL,
                    metadata TEXT DEFAULT '{}'
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_session
                ON messages(session_id, id DESC)
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_timestamp
                ON messages(timestamp)
            """)
            # 兼容旧表：补齐 sender_id, sender_name
            for col in ["sender_id", "sender_name"]:
                try:
                    await db.execute(f"ALTER TABLE messages ADD COLUMN {col} TEXT DEFAULT ''")
                except Exception:
                    pass
            await db.commit()

    async def add_message(self, session_id: str, user_id: str, role: str, content: str,
                           sender_name: str = ""):
        """添加一条消息（单条写入，保留以兼容旧代码）"""
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
                "INSERT INTO messages(session_id, role, content, sender_id, sender_name, timestamp) VALUES (?,?,?,?,?,?)",
                (session_id, role, content, user_id, sender_name, now),
            )
            await db.commit()

    async def batch_add_messages(self, messages: list[dict]) -> int:
        """批量写入消息（热缓存刷写用）

        Args:
            messages: list of dicts with keys:
                      session_id, user_id, role, content, sender_name, timestamp

        Returns:
            写入条数
        """
        if not messages:
            return 0
        now = time.time()

        # 按 session_id 聚合（减少 session 更新次数）
        sessions_info: dict[str, dict] = {}
        for msg in messages:
            sid = msg.get("session_id", "default")
            uid = msg.get("user_id", "")
            ts = msg.get("timestamp", now)
            if sid not in sessions_info:
                sessions_info[sid] = {
                    "user_id": uid,
                    "first_ts": ts,
                    "last_ts": ts,
                    "count": 0,
                }
            info = sessions_info[sid]
            info["last_ts"] = max(info["last_ts"], ts)
            info["first_ts"] = min(info["first_ts"], ts)
            info["count"] += 1

        async with self._connect() as db:
            for sid, info in sessions_info.items():
                await db.execute(
                    "INSERT OR IGNORE INTO sessions(session_id, user_id, created_at, last_active_at) "
                    "VALUES (?,?,?,?)",
                    (sid, info["user_id"], info["first_ts"], info["last_ts"]),
                )
                await db.execute(
                    "UPDATE sessions SET last_active_at=MAX(last_active_at,?), "
                    "message_count=message_count+? WHERE session_id=?",
                    (info["last_ts"], info["count"], sid),
                )

            for msg in messages:
                await db.execute(
                    "INSERT INTO messages(session_id, role, content, sender_id, sender_name, timestamp) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        msg.get("session_id", "default"),
                        msg["role"],
                        msg["content"],
                        msg.get("user_id", ""),
                        msg.get("sender_name", ""),
                        msg.get("timestamp", now),
                    ),
                )
            await db.commit()

        # 每次批量写入后立即滑动窗口，保持冷备份不超过上限
        try:
            await self.enforce_retention()
        except Exception:
            pass

        return len(messages)

    async def enforce_retention(self, max_per_user: int = 1000) -> int:
        """滑动窗口：每用户只保留最新 N 条消息，超出自动丢弃最旧的

        这是一个"滑动窗口"——新的进来时，最旧的被挤出去。
        按 sender_id 分组计数，DELET 超出部分的最老消息。

        Args:
            max_per_user: 每用户最大保留条数（默认 1000）

        Returns:
            本次丢弃的消息总数
        """
        total = 0

        async with self._connect() as db:
            users = await db.execute_fetchall(
                "SELECT DISTINCT sender_id FROM messages",
            )
            for (uid,) in users:
                count = (await db.execute_fetchall(
                    "SELECT COUNT(*) FROM messages WHERE sender_id=?",
                    (uid,),
                ))[0][0]
                if count <= max_per_user:
                    continue
                excess = count - max_per_user
                # 删除该用户最旧的 excess 条消息
                cur = await db.execute(
                    "DELETE FROM messages WHERE sender_id=? AND id IN ("
                    "SELECT id FROM messages WHERE sender_id=? ORDER BY id ASC LIMIT ?"
                    ")", (uid, uid, excess),
                )
                total += cur.rowcount or 0

            # 清理孤立 session
            await db.execute("""
                DELETE FROM sessions WHERE session_id NOT IN (
                    SELECT DISTINCT session_id FROM messages
                )
            """)
            await db.commit()

        return total

    # ── 读取 ──

    async def get_recent_context(self, session_id: str, limit: int = 20,
                                  bot_name: str = "我") -> str:
        """获取最近的对话上下文（用于写日记/判断）

        格式：[时间] 昵称: 消息，时间按距今天数自适应：
        - 当天 → [HH:MM]
        - 跨天但≤7天 → [MM-DD HH:MM]
        - >7天 → [MM-DD]
        """
        from ..utils.context_formatter import format_msg
        async with self._connect() as db:
            rows = await db.execute_fetchall(
                "SELECT role, content, sender_name, sender_id, timestamp "
                "FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            )
        if not rows:
            return ""
        now = time.time()
        lines = []
        for r in reversed(rows):
            role = r[0]
            content = r[1]
            name = r[2] or ""
            sid = r[3] or ""
            ts = r[4] or now
            if role == "user":
                display = name if name else (sid or "用户")
            else:
                display = f"Bot: {bot_name}"
            lines.append(format_msg(ts, display, content, now))
        return "\n".join(lines)

    async def get_context_since(self, session_id: str, after_id: int = 0,
                                 limit: int = 50, bot_name: str = "我") -> str:
        """获取自指定消息 ID 之后的对话上下文（滑窗追踪）

        格式同 get_recent_context。
        after_id=0 时退化为 get_recent_context（取最新 limit 条）。
        """
        from ..utils.context_formatter import format_msg
        async with self._connect() as db:
            if after_id > 0:
                rows = await db.execute_fetchall(
                    "SELECT role, content, sender_name, sender_id, timestamp, id "
                    "FROM messages WHERE session_id=? AND id>? ORDER BY id ASC LIMIT ?",
                    (session_id, after_id, limit),
                )
            else:
                rows = await db.execute_fetchall(
                    "SELECT role, content, sender_name, sender_id, timestamp, id "
                    "FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
                    (session_id, limit),
                )
                rows = list(reversed(rows))
        if not rows:
            return ""
        now = time.time()
        lines = []
        for r in rows:
            role, content, name, sid, ts = r[0], r[1], r[2] or "", r[3] or "", r[4] or now
            if role == "user":
                display = name if name else (sid or "用户")
            else:
                display = f"Bot: {bot_name}"
            lines.append(format_msg(ts, display, content, now))
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
