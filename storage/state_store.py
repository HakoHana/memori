"""会话状态持久化 — SQLite 存储"""

from __future__ import annotations

from ..models.memory_atom import PersistedSessionState
from .base_store import BaseDbStore


class StateStore(BaseDbStore):
    """保存/恢复每个用户的 ConsolidationManager 状态"""

    async def initialize(self):
        async with self._connect() as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS consolidation_state (
                    user_id TEXT PRIMARY KEY,
                    msg_count INTEGER DEFAULT 0,
                    warmup_threshold INTEGER DEFAULT 1,
                    last_consolidated_at REAL,
                    last_diary_date TEXT,
                    diary_count INTEGER DEFAULT 0,
                    diary_count_since_persona INTEGER DEFAULT 0,
                    l1_retry_count INTEGER DEFAULT 0
                )
            """)
            await db.commit()

    async def save(self, state: PersistedSessionState):
        """持久化状态"""
        async with self._connect() as db:
            await db.execute("""
                INSERT OR REPLACE INTO consolidation_state
                (user_id, msg_count, warmup_threshold, last_consolidated_at,
                 last_diary_date, diary_count, diary_count_since_persona, l1_retry_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                state.user_id, state.msg_count, state.warmup_threshold,
                state.last_consolidated_at, state.last_diary_date,
                state.diary_count, state.diary_count_since_persona,
                state.l1_retry_count,
            ))
            await db.commit()

    async def load(self, user_id: str) -> PersistedSessionState | None:
        """加载用户状态"""
        async with self._connect() as db:
            rows = await db.execute_fetchall(
                "SELECT * FROM consolidation_state WHERE user_id = ?",
                (user_id,),
            )
        if not rows:
            return None
        row = rows[0]
        return PersistedSessionState(
            user_id=row[0],
            msg_count=row[1],
            warmup_threshold=row[2],
            last_consolidated_at=row[3] or 0.0,
            last_diary_date=row[4] or "",
            diary_count=row[5],
            diary_count_since_persona=row[6],
            l1_retry_count=row[7],
        )

    async def load_all(self) -> dict[str, PersistedSessionState]:
        """加载所有用户状态（启动时恢复）"""
        async with self._connect() as db:
            rows = await db.execute_fetchall("SELECT * FROM consolidation_state")
        states = {}
        for row in rows:
            states[row[0]] = PersistedSessionState(
                user_id=row[0],
                msg_count=row[1],
                warmup_threshold=row[2],
                last_consolidated_at=row[3] or 0.0,
                last_diary_date=row[4] or "",
                diary_count=row[5],
                diary_count_since_persona=row[6],
                l1_retry_count=row[7],
            )
        return states

    async def delete(self, user_id: str):
        """删除用户状态"""
        async with self._connect() as db:
            await db.execute(
                "DELETE FROM consolidation_state WHERE user_id = ?", (user_id,)
            )
            await db.commit()
