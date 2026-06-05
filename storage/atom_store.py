"""SQLite 原子存储 — 实现 MemoryStore 接口"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from typing import Any

import aiosqlite

from ..models.memory_atom import MemoryAtom, AtomType, AtomStatus, DecayType


class AtomStore:
    """原子存储：SQLite + FTS5 全文搜索"""

    def __init__(self, db_path: str):
        self.db_path = db_path

    @asynccontextmanager
    async def _connect(self):
        db = await aiosqlite.connect(self.db_path)
        try:
            await db.execute("PRAGMA journal_mode = WAL")
            await db.execute("PRAGMA busy_timeout = 5000")
            await db.execute("PRAGMA foreign_keys = ON")
            yield db
        finally:
            await db.close()

    async def initialize(self):
        """建表 + FTS5 索引"""
        async with self._connect() as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS memory_atoms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    diary_date TEXT NOT NULL,
                    atom_type TEXT NOT NULL DEFAULT 'unknown',
                    content TEXT NOT NULL,
                    entities TEXT DEFAULT '[]',
                    importance REAL NOT NULL DEFAULT 0.5,
                    confidence REAL NOT NULL DEFAULT 0.7,
                    access_count INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    last_accessed_at REAL,
                    ttl_days REAL NOT NULL DEFAULT 30.0,
                    expires_at REAL NOT NULL DEFAULT 0.0,
                    decay_type TEXT NOT NULL DEFAULT 'exponential',
                    status TEXT NOT NULL DEFAULT 'active',
                    session_id TEXT,
                    diary_ref TEXT,
                    diary_snippet TEXT DEFAULT '',
                    embedding BLOB,
                    embedding_model TEXT,
                    metadata TEXT DEFAULT '{}'
                )
            """)
            await db.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_atoms_fts
                USING fts5(content, atom_id UNINDEXED, user_id UNINDEXED, tokenize='unicode61')
            """)

            # ── 复合索引（匹配实际查询模式）──
            for idx in [
                # ① 按用户查活跃原子，按日期排序（get_by_user, /日记 列表）
                "CREATE INDEX IF NOT EXISTS idx_atoms_user_status_date "
                "ON memory_atoms(user_id, status, diary_date)",

                # ② 按用户查活跃原子，按重要度降序（召回时取最重要的）
                "CREATE INDEX IF NOT EXISTS idx_atoms_user_status_imp "
                "ON memory_atoms(user_id, status, importance DESC)",

                # ③ 按用户+类型分组（统计、展示分组）
                "CREATE INDEX IF NOT EXISTS idx_atoms_user_type "
                "ON memory_atoms(user_id, atom_type)",

                # ④ 预留：TTL 过期清理（以后衰减用）
                "CREATE INDEX IF NOT EXISTS idx_atoms_status_ttl "
                "ON memory_atoms(status, ttl_days)",

                # ⑤ FTS5 搜索时按用户过滤
                "CREATE INDEX IF NOT EXISTS idx_atoms_user "
                "ON memory_atoms(user_id)",
            ]:
                await db.execute(idx)

            # 兼容老数据库：添加缺失的列
            for col_def in [
                "ALTER TABLE memory_atoms ADD COLUMN diary_snippet TEXT DEFAULT ''",
                "ALTER TABLE memory_atoms ADD COLUMN expires_at REAL NOT NULL DEFAULT 0.0",
                "ALTER TABLE memory_atoms ADD COLUMN decay_type TEXT NOT NULL DEFAULT 'exponential'",
            ]:
                try:
                    await db.execute(col_def)
                except Exception:
                    pass  # 列已存在

            await db.commit()

    async def insert(self, atom: MemoryAtom) -> int:
        """插入单条原子，返回 ID"""
        async with self._connect() as db:
            cursor = await db.execute("""
                INSERT INTO memory_atoms
                (user_id, diary_date, atom_type, content, entities,
                 importance, confidence, access_count, created_at,
                 last_accessed_at, ttl_days, expires_at, decay_type, status,
                 session_id, diary_ref, diary_snippet, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                atom.user_id, atom.diary_date, atom.atom_type.value, atom.content,
                json.dumps(atom.entities, ensure_ascii=False),
                atom.importance, atom.confidence, atom.access_count,
                atom.created_at, atom.last_accessed_at, atom.ttl_days,
                atom.expires_at, atom.decay_type.value, atom.status.value,
                atom.session_id, atom.diary_ref,
                atom.diary_snippet,
                json.dumps(atom.metadata, ensure_ascii=False),
            ))
            atom_id = cursor.lastrowid

            await db.execute(
                "INSERT INTO memory_atoms_fts (atom_id, content, user_id) VALUES (?, ?, ?)",
                (atom_id, atom.content, atom.user_id),
            )
            await db.commit()
            return atom_id

    async def insert_many(self, atoms: list[MemoryAtom]) -> list[int]:
        """批量插入原子"""
        if not atoms:
            return []
        ids = []
        async with self._connect() as db:
            for atom in atoms:
                cursor = await db.execute("""
                    INSERT INTO memory_atoms
                    (user_id, diary_date, atom_type, content, entities,
                     importance, confidence, access_count, created_at,
                     last_accessed_at, ttl_days, expires_at, decay_type, status,
                     session_id, diary_ref, diary_snippet, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    atom.user_id, atom.diary_date, atom.atom_type.value, atom.content,
                    json.dumps(atom.entities, ensure_ascii=False),
                    atom.importance, atom.confidence, atom.access_count,
                    atom.created_at, atom.last_accessed_at, atom.ttl_days,
                    atom.expires_at, atom.decay_type.value, atom.status.value,
                    atom.session_id, atom.diary_ref,
                    atom.diary_snippet,
                    json.dumps(atom.metadata, ensure_ascii=False),
                ))
                atom_id = cursor.lastrowid
                ids.append(atom_id)
                await db.execute(
                    "INSERT INTO memory_atoms_fts (atom_id, content, user_id) VALUES (?, ?, ?)",
                    (atom_id, atom.content, atom.user_id),
                )
            await db.commit()
        return ids

    async def search_fts(self, query: str, user_id: str, k: int = 5) -> list[MemoryAtom]:
        """FTS5 全文搜索原子"""
        async with self._connect() as db:
            # FTS5 查询：转义特殊字符，加前缀匹配
            safe_query = self._sanitize_fts_query(query)
            if not safe_query:
                return []

            rows = await db.execute_fetchall("""
                SELECT a.* FROM memory_atoms a
                JOIN memory_atoms_fts f ON a.id = f.atom_id
                WHERE memory_atoms_fts MATCH ? AND a.user_id = ? AND a.status = 'active'
                ORDER BY rank
                LIMIT ?
            """, (safe_query, user_id, k))

        return [self._row_to_atom(row) for row in rows]

    async def touch(self, atom_id: int):
        """更新访问时间和计数"""
        async with self._connect() as db:
            await db.execute("""
                UPDATE memory_atoms
                SET last_accessed_at = ?, access_count = access_count + 1
                WHERE id = ?
            """, (time.time(), atom_id))
            await db.commit()

    async def delete(self, atom_id: int, user_id: str) -> bool:
        """软删除：标记状态为 forgotten"""
        async with self._connect() as db:
            cursor = await db.execute("""
                UPDATE memory_atoms SET status = 'forgotten' WHERE id = ? AND user_id = ?
            """, (atom_id, user_id))
            await db.commit()
            return cursor.rowcount > 0

    async def get_by_user(self, user_id: str, status: str | None = "active") -> list[MemoryAtom]:
        """获取用户的所有原子"""
        async with self._connect() as db:
            if status:
                rows = await db.execute_fetchall(
                    "SELECT * FROM memory_atoms WHERE user_id = ? AND status = ? ORDER BY created_at DESC",
                    (user_id, status),
                )
            else:
                rows = await db.execute_fetchall(
                    "SELECT * FROM memory_atoms WHERE user_id = ? ORDER BY created_at DESC",
                    (user_id,),
                )
        return [self._row_to_atom(row) for row in rows]

    async def get_by_id(self, atom_id: int) -> MemoryAtom | None:
        """按 ID 获取原子"""
        async with self._connect() as db:
            rows = await db.execute_fetchall(
                "SELECT * FROM memory_atoms WHERE id = ?", (atom_id,),
            )
        if not rows:
            return None
        return self._row_to_atom(rows[0])

    async def get_stats(self, user_id: str) -> dict[str, Any]:
        """获取用户记忆统计"""
        async with self._connect() as db:
            total = (await db.execute_fetchall(
                "SELECT COUNT(*) FROM memory_atoms WHERE user_id = ?", (user_id,)
            ))[0][0]
            by_type = await db.execute_fetchall("""
                SELECT atom_type, COUNT(*) FROM memory_atoms
                WHERE user_id = ? AND status = 'active'
                GROUP BY atom_type
            """, (user_id,))
        return {
            "total": total,
            "by_type": dict(by_type),
        }

    async def apply_decay(self, decay_rate: float = 0.99):
        """对所有原子应用重要性衰减（预留接口）"""
        async with self._connect() as db:
            await db.execute(
                "UPDATE memory_atoms SET importance = importance * ? WHERE status = 'active'",
                (decay_rate,),
            )
            await db.commit()

    async def get_all_active_user_ids(self) -> list[str]:
        """获取所有有活跃原子的用户 ID"""
        async with self._connect() as db:
            rows = await db.execute_fetchall(
                "SELECT DISTINCT user_id FROM memory_atoms WHERE status = 'active'"
            )
        return [r[0] for r in rows]

    # ── 内部工具 ──

    def _sanitize_fts_query(self, query: str) -> str:
        """清理 FTS5 查询字符串"""
        if not query or not query.strip():
            return ""
        # 移除特殊字符，FTS5 默认支持前缀匹配
        import re
        cleaned = re.sub(r'[^\w一-鿿]', ' ', query)
        cleaned = cleaned.strip()
        if not cleaned:
            return ""
        # 对每个词加 * 前缀匹配
        terms = cleaned.split()
        return ' AND '.join(f'"{t}"*' for t in terms if t)

    def _row_to_atom(self, row) -> MemoryAtom:
        """数据库行 → MemoryAtom"""
        return MemoryAtom(
            atom_id=row[0],
            user_id=row[1],
            diary_date=row[2],
            atom_type=AtomType(row[3]),
            content=row[4],
            entities=json.loads(row[5]) if isinstance(row[5], str) else (row[5] or []),
            importance=row[6],
            confidence=row[7],
            access_count=row[8],
            created_at=row[9],
            last_accessed_at=row[10],
            ttl_days=row[11],
            expires_at=row[12] or 0.0,
            decay_type=DecayType(row[13]) if row[13] else DecayType.EXPONENTIAL,
            status=AtomStatus(row[14]),
            session_id=row[15],
            diary_ref=row[16],
            diary_snippet=row[17] or "",
            metadata=json.loads(row[20]) if len(row) > 20 and isinstance(row[20], str) and row[20] else {},
        )
