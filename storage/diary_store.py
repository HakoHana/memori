"""日记存储 — SQLite 实现"""

from __future__ import annotations

import json
import time
from datetime import datetime

from .base_store import BaseDbStore


class DiaryStore(BaseDbStore):
    """日记存储：全部存在 SQLite 的 diary_entries 表中"""
    # 默认 PRAGMA 够用，不需要额外设置

    async def initialize(self):
        """创建 diary_entries 表"""
        async with self._connect() as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS diary_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    date TEXT NOT NULL,
                    content TEXT NOT NULL,
                    topics TEXT DEFAULT '[]',
                    sentiment TEXT DEFAULT '',
                    importance REAL DEFAULT 0.5,
                    atom_count INTEGER DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    status TEXT DEFAULT 'active'
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_diary_user_date
                ON diary_entries(user_id, date)
            """)
            # 列补齐已在 db_migration.py 中集中管理
            await db.commit()

    async def append(self, user_id: str, date_str: str, content: str) -> int:
        """追加内容到当日日记（追加到已有条目末尾，或创建新条目）

        Returns:
            日记条目的 ID
        """
        now = time.time()
        async with self._connect() as db:
            row = await db.execute_fetchall(
                "SELECT id, content FROM diary_entries WHERE user_id = ? AND date = ?",
                (user_id, date_str),
            )
            if row:
                entry_id, old_content = row[0]
                time_tag = datetime.now().strftime("%H:%M")
                new_content = f"{old_content}\n\n## {time_tag}\n\n{content.strip()}"
                await db.execute(
                    "UPDATE diary_entries SET content = ?, updated_at = ? WHERE id = ?",
                    (new_content, now, entry_id),
                )
            else:
                cursor = await db.execute("""
                    INSERT INTO diary_entries
                    (user_id, date, content, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (user_id, date_str, content.strip(), now, now))
                entry_id = cursor.lastrowid
            await db.commit()
            return entry_id

    async def read(self, user_id: str, date_str: str) -> str | None:
        """读取某天的日记"""
        async with self._connect() as db:
            rows = await db.execute_fetchall(
                "SELECT content FROM diary_entries WHERE user_id = ? AND date = ?",
                (user_id, date_str),
            )
        return rows[0][0] if rows else None

    async def list_months(self, user_id: str) -> list[dict[str, str]]:
        """列出所有有日记的年月"""
        async with self._connect() as db:
            rows = await db.execute_fetchall("""
                SELECT DISTINCT substr(date, 1, 4) as year, substr(date, 6, 2) as month
                FROM diary_entries WHERE user_id = ?
                ORDER BY year DESC, month DESC
            """, (user_id,))
        return [{"year": r[0], "month": r[1]} for r in rows]

    async def list_dates(self, user_id: str, year: str, month: str) -> list[dict]:
        """列出某个月份所有日记日期"""
        prefix = f"{year}-{month}"
        async with self._connect() as db:
            rows = await db.execute_fetchall("""
                SELECT date FROM diary_entries
                WHERE user_id = ? AND date LIKE ?
                ORDER BY date DESC
            """, (user_id, f"{prefix}%"))
        return [{"date": r[0]} for r in rows]

    async def delete_date(self, user_id: str, date_str: str) -> bool:
        """删除某天的日记"""
        async with self._connect() as db:
            cursor = await db.execute(
                "DELETE FROM diary_entries WHERE user_id = ? AND date = ?",
                (user_id, date_str),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def get_all_user_ids(self) -> list[str]:
        """获取所有有日记的用户 ID"""
        async with self._connect() as db:
            rows = await db.execute_fetchall(
                "SELECT DISTINCT user_id FROM diary_entries"
            )
        return [r[0] for r in rows]

    async def upsert(self, user_id: str, date_str: str, content: str) -> bool:
        """插入或更新日记（page_api 用）"""
        import time
        now = time.time()
        async with self._connect() as db:
            row = await db.execute_fetchall(
                "SELECT id FROM diary_entries WHERE user_id=? AND date=?", (user_id, date_str)
            )
            if row:
                await db.execute(
                    "UPDATE diary_entries SET content=?, updated_at=? WHERE id=?",
                    (content, now, row[0][0]),
                )
            else:
                await db.execute(
                    "INSERT INTO diary_entries(user_id,date,content,created_at,updated_at) VALUES(?,?,?,?,?)",
                    (user_id, date_str, content, now, now),
                )
            await db.commit()
            return True

    async def update_metadata(self, user_id: str, date_str: str, **kwargs):
        """更新日记的元数据（话题、情感等）"""
        sets = []
        vals = []
        for key, val in kwargs.items():
            if key in ("topics", "sentiment", "importance", "atom_count"):
                sets.append(f"{key} = ?")
                vals.append(val)
        if not sets:
            return
        vals.append(user_id)
        vals.append(date_str)
        async with self._connect() as db:
            await db.execute(
                f"UPDATE diary_entries SET {', '.join(sets)} WHERE user_id = ? AND date = ?",
                vals,
            )
            await db.commit()
