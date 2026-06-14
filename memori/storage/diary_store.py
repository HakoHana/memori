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
            # 增量同步 FTS（只索引新条目，不再全量 rebuild）
            try:
                await db.execute(
                    "INSERT INTO diary_fts(rowid, content) VALUES (?, '')",
                    (entry_id,),
                )
                await db.commit()
            except Exception:
                pass
            return entry_id

    async def read(self, user_id: str, date_str: str) -> str | None:
        """读取某天最新一条日记"""
        rows = await self.fetch(
            "SELECT content FROM diary_entries WHERE user_id = ? AND date = ? ORDER BY id DESC LIMIT 1",
            (user_id, date_str),
        )
        return rows[0][0] if rows else None

    async def read_all(self, user_id: str, date_str: str) -> list[dict]:
        """读取某天所有日记条目（按时间正序）"""
        rows = await self.fetch("""
            SELECT id, content, topics, sentiment, importance, created_at
            FROM diary_entries WHERE user_id = ? AND date = ?
            ORDER BY id ASC
        """, (user_id, date_str))
        return [
            {
                "id": r[0], "content": r[1], "topics": r[2],
                "sentiment": r[3], "importance": r[4],
                "created_at": r[5],
            }
            for r in rows
        ]

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

    async def upsert(self, user_id: str, date_str: str, content: str) -> int:
        """追加一篇日记（一天多份，不覆盖已有）

        Returns:
            新日记的 ID
        """
        return await self.append(user_id, date_str, content)

    async def update_metadata(self, user_id: str, date_str: str, **kwargs):
        """更新某天最新一条日记的元数据

        一天多份日记时精确匹配最新条目，不会误改历史条目。
        """
        sets = []
        vals = []
        for key, val in kwargs.items():
            if key in ("topics", "sentiment", "importance", "atom_count"):
                sets.append(f"{key} = ?")
                vals.append(val)
        if not sets:
            return
        row = await self.fetchone(
            "SELECT id FROM diary_entries WHERE user_id=? AND date=? ORDER BY id DESC LIMIT 1",
            (user_id, date_str),
        )
        if not row:
            return
        vals.append(row[0])
        await self.execute(
            f"UPDATE diary_entries SET {', '.join(sets)} WHERE id = ?",
            vals,
        )

    async def update_metadata_by_id(self, entry_id: int, **kwargs):
        """按日记条目 ID 更新元数据（精确操作，不受多条目影响）"""
        sets = []
        vals = []
        for key, val in kwargs.items():
            if key in ("topics", "sentiment", "importance", "atom_count"):
                sets.append(f"{key} = ?")
                vals.append(val)
        if not sets:
            return
        vals.append(entry_id)
        await self.execute(
            f"UPDATE diary_entries SET {', '.join(sets)} WHERE id = ?",
            vals,
        )

    # ── 通用查询（替代 routes.py 中的裸 SQL） ────────────

    async def list_paginated(
        self, uid: str | None = None, page: int = 1, size: int = 20
    ) -> tuple[list[dict], int]:
        """分页日记列表

        Returns:
            (items, total_count)
        """
        offset = (page - 1) * size
        columns = ("id", "user_id", "date", "content", "importance", "sentiment", "topics", "created_at")

        if uid:
            rows = await self.fetch(
                f"SELECT {', '.join(columns)} FROM diary_entries "
                "WHERE user_id=? ORDER BY date DESC LIMIT ? OFFSET ?",
                (uid, size, offset),
            )
            total = (await self.fetchone(
                "SELECT COUNT(*) FROM diary_entries WHERE user_id=?", (uid,)
            ))[0]
        else:
            rows = await self.fetch(
                f"SELECT {', '.join(columns)} FROM diary_entries "
                "ORDER BY date DESC LIMIT ? OFFSET ?",
                (size, offset),
            )
            total = (await self.fetchone("SELECT COUNT(*) FROM diary_entries"))[0]

        items = []
        for r in rows:
            topics_raw = r[6]
            topics = []
            if topics_raw:
                try:
                    import json
                    topics = json.loads(topics_raw) if isinstance(topics_raw, str) else topics_raw
                except Exception:
                    topics = [str(topics_raw)]
            items.append({
                "id": r[0], "user_id": r[1], "date": r[2], "content": r[3],
                "importance": r[4], "sentiment": r[5], "topics": topics,
                "created_at": r[7],
            })
        return items, total

    async def get_by_id(self, entry_id: int) -> dict | None:
        """按 ID 获取日记条目（动态列映射，不硬编码）"""
        row = await self.fetchone("SELECT * FROM diary_entries WHERE id=?", (entry_id,))
        if not row:
            return None
        # 从 cursor 描述动态获取列名，避免硬编码与表结构不同步
        import aiosqlite
        # 获取 PRAGMA table_info 得到真实列名
        col_rows = await self.fetch("PRAGMA table_info(diary_entries)")
        columns = [r[1] for r in col_rows] if col_rows else []
        if not columns:
            # fallback 硬编码
            columns = ["id", "user_id", "date", "content", "topics", "sentiment",
                       "importance", "atom_count", "created_at", "updated_at",
                       "status", "archived"]
        return dict(zip(columns, row))

    async def count(self, uid: str | None = None) -> int:
        """日记条目计数"""
        if uid:
            row = await self.fetchone(
                "SELECT COUNT(*) FROM diary_entries WHERE user_id=?", (uid,)
            )
        else:
            row = await self.fetchone("SELECT COUNT(*) FROM diary_entries")
        return row[0] if row else 0

    async def get_timeline_dates(self, uid: str, year: str = "", month: str = "") -> list[str]:
        """获取时间线日期列表"""
        if not uid:
            return []
        if year and month:
            ym = f"{year}-{int(month):02d}"
            rows = await self.fetch(
                "SELECT DISTINCT date FROM diary_entries WHERE user_id=? AND date LIKE ? ORDER BY date DESC",
                (uid, f"{ym}%"),
            )
        elif year:
            rows = await self.fetch(
                "SELECT DISTINCT date FROM diary_entries WHERE user_id=? AND date LIKE ? ORDER BY date DESC",
                (uid, f"{year}%"),
            )
        else:
            rows = await self.fetch(
                "SELECT DISTINCT date FROM diary_entries WHERE user_id=? ORDER BY date DESC LIMIT 100",
                (uid,),
            )
        return [r[0] for r in rows]
