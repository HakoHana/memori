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
                    status TEXT DEFAULT 'active',
                    archived INTEGER DEFAULT 0
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_diary_user_date
                ON diary_entries(user_id, date)
            """)
            # FTS5 索引（自动同步 diary_entries.content）
            try:
                await db.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS diary_fts
                    USING fts5(content, content=diary_entries, tokenize='unicode61')
                """)
                # 重建 FTS 索引（确保已有数据也被索引）
                try:
                    await db.execute("INSERT INTO diary_fts(diary_fts) VALUES('rebuild')")
                except Exception:
                    pass
            except Exception:
                pass

            # 列补齐已在 db_migration.py 中集中管理
            await db.commit()

    async def search_fts(self, query: str, user_id: str, k: int = 5,
                          imp_weight: float = 0.6, rank_weight: float = 0.4) -> list[dict]:
        """在 diary_entries 上全文搜索

        Args:
            query: 搜索关键词
            user_id: 用户 ID（空字符串则查所有）
            k: 返回条数
            imp_weight: 重要度权重
            rank_weight: 匹配度权重
        """
        safe_query = self._sanitize_fts_query(query)
        if not safe_query:
            return []

        candidates = k * 3
        if user_id:
            rows = await self.fetch("""
                SELECT d.id, d.date, d.content, d.importance, rank
                FROM diary_fts
                JOIN diary_entries d ON diary_fts.rowid = d.id
                WHERE diary_fts MATCH ? AND d.user_id = ?
                ORDER BY rank
                LIMIT ?
            """, (safe_query, user_id, candidates))
        else:
            rows = await self.fetch("""
                SELECT d.id, d.date, d.content, d.importance, rank
                FROM diary_fts
                JOIN diary_entries d ON diary_fts.rowid = d.id
                WHERE diary_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (safe_query, candidates))

        if not rows:
            return []

        max_rank = abs(rows[-1][-1]) if rows else 1
        if max_rank < 0.001:
            max_rank = 1

        scored = []
        for r in rows:
            rank_val = abs(r[-1])
            rank_norm = 1.0 - min(1.0, rank_val / max_rank)
            score = (r[3] or 0.5) * imp_weight + rank_norm * rank_weight
            scored.append((score, {
                "id": r[0], "date": r[1], "content": r[2][:200],
                "importance": r[3], "score": round(score, 3),
            }))

        scored.sort(key=lambda x: -x[0])
        return [s[1] for s in scored[:k]]

    def _sanitize_fts_query(self, query: str) -> str:
        if not query or not query.strip():
            return ""
        import re
        cleaned = re.sub(r'[^\w一-鿿]', ' ', query).strip()
        if not cleaned:
            return ""
        terms = cleaned.split()
        return ' AND '.join(f'"{t}"*' for t in terms if t)

    async def append(self, user_id: str, date_str: str, content: str) -> int:
        """写入一条日记 — 每条独立插入，不按日期去重

        Returns:
            日记条目的 ID
        """
        now = time.time()
        async with self._connect() as db:
            cursor = await db.execute("""
                INSERT INTO diary_entries
                (user_id, date, content, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
            """, (user_id, date_str, content.strip(), now, now))
            entry_id = cursor.lastrowid
            await db.commit()
            # 重建 FTS
            try:
                await db.execute("INSERT INTO diary_fts(diary_fts) VALUES('rebuild')")
            except Exception:
                pass
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
        """插入日记 — 每次都新建条目（不按日期去重）"""
        import time
        now = time.time()
        async with self._connect() as db:
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
