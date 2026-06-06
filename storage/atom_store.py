"""SQLite 原子存储 — 实现 MemoryStore 接口"""

from __future__ import annotations

import json
import time
from typing import Any

from ..models.memory_atom import MemoryAtom, AtomType, AtomStatus, DecayType
from .base_store import BaseDbStore


# 插入 SQL（预定义，避免重复拼接）
_INSERT_SQL = """\
INSERT INTO memory_atoms
(user_id, diary_date, atom_type, content, entities,
 importance, confidence, access_count, created_at,
 last_accessed_at, ttl_days, expires_at, decay_type, status,
 session_id, diary_ref, diary_snippet, metadata, diary_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\
"""

_INSERT_FTS_SQL = """\
INSERT INTO memory_atoms_fts (atom_id, content, user_id) VALUES (?, ?, ?)\
"""


class AtomStore(BaseDbStore):
    """原子存储：SQLite + FTS5 全文搜索"""
    _pragmas = [
        "PRAGMA journal_mode = WAL",
        "PRAGMA synchronous = NORMAL",
        "PRAGMA cache_size = -8000",
    ]

    async def initialize(self):
        """建表 + FTS5 索引 + 复合索引"""
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
                    metadata TEXT DEFAULT '{}',
                    diary_id INTEGER DEFAULT 0
                )
            """)
            await db.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_atoms_fts
                USING fts5(content, atom_id UNINDEXED, user_id UNINDEXED, tokenize='unicode61')
            """)

            # 兼容旧数据库：补齐 diary_id 列
            try:
                await db.execute("ALTER TABLE memory_atoms ADD COLUMN diary_id INTEGER DEFAULT 0")
            except Exception:
                pass

            # 全局事实表 + 日记关联表（v4 去重架构）
            await db.execute("""
                CREATE TABLE IF NOT EXISTS atomic_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    atom_type TEXT NOT NULL DEFAULT 'unknown',
                    importance REAL NOT NULL DEFAULT 0.5,
                    confidence REAL NOT NULL DEFAULT 0.7,
                    source_count INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_af_content ON atomic_facts(content)"
            )
            await db.execute("""
                CREATE TABLE IF NOT EXISTS diary_fact_links (
                    diary_id INTEGER NOT NULL,
                    fact_id INTEGER NOT NULL,
                    importance REAL DEFAULT 0.5,
                    snippet TEXT DEFAULT '',
                    PRIMARY KEY (diary_id, fact_id)
                )
            """)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_dfl_diary ON diary_fact_links(diary_id)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_dfl_fact ON diary_fact_links(fact_id)"
            )

            for idx in [
                "CREATE INDEX IF NOT EXISTS idx_atoms_user_status_date ON memory_atoms(user_id, status, diary_date)",
                "CREATE INDEX IF NOT EXISTS idx_atoms_user_status_imp ON memory_atoms(user_id, status, importance DESC)",
                "CREATE INDEX IF NOT EXISTS idx_atoms_user_type ON memory_atoms(user_id, atom_type)",
                "CREATE INDEX IF NOT EXISTS idx_atoms_status_ttl ON memory_atoms(status, ttl_days)",
                "CREATE INDEX IF NOT EXISTS idx_atoms_user ON memory_atoms(user_id)",
            ]:
                await db.execute(idx)

            # 列补齐已在 db_migration.py 中集中管理
            await db.commit()

    # ═══════════════════════════════════════════════════
    #  插入（单个 + 批量）
    # ═══════════════════════════════════════════════════

    async def insert(self, atom: MemoryAtom) -> int:
        """插入单条原子，返回 ID"""
        async with self._connect() as db:
            cursor = await db.execute(_INSERT_SQL, self._atom_values(atom))
            atom_id = cursor.lastrowid
            await db.execute(
                _INSERT_FTS_SQL,
                (atom_id, atom.content, atom.user_id),
            )
            await db.commit()
            return atom_id

    async def insert_many(self, atoms: list[MemoryAtom]) -> list[int]:
        """批量插入原子（共享连接 + 事务内循环，避免重复 open/close）"""
        if not atoms:
            return []

        ids: list[int] = []
        async with self._connect() as db:
            for atom in atoms:
                c = await db.execute(_INSERT_SQL, self._atom_values(atom))
                aid = c.lastrowid
                ids.append(aid)
                await db.execute(
                    _INSERT_FTS_SQL, (aid, atom.content, atom.user_id),
                )
            await db.commit()

        for atom, aid in zip(atoms, ids):
            atom.atom_id = aid

        return ids

    # ═══════════════════════════════════════════════════
    #  查询
    # ═══════════════════════════════════════════════════

    async def search_fts(self, query: str, user_id: str, k: int = 5) -> list[MemoryAtom]:
        """FTS5 全文搜索（按重要度 × BM25 排序）"""
        safe_query = self._sanitize_fts_query(query)
        if not safe_query:
            return []

        # 多取一些候选，按重要度降序重排，让高重要度的匹配优先
        candidates = k * 3
        rows = await self.fetch("""
            SELECT a.*, rank FROM memory_atoms a
            JOIN memory_atoms_fts f ON a.id = f.atom_id
            WHERE memory_atoms_fts MATCH ? AND a.user_id = ? AND a.status = 'active'
            ORDER BY rank
            LIMIT ?
        """, (safe_query, user_id, candidates))

        if not rows:
            return []

        # 按 (importance × 0.6 + rank_normalized × 0.4) 排序
        # rank 是负值（越接近0越匹配），归一化到 0~1
        max_rank = abs(rows[-1][-1]) if rows else 1  # 最差匹配的绝对值
        if max_rank < 0.001:
            max_rank = 1

        scored = []
        for r in rows:
            atom = self._row_to_atom(r)
            rank_val = abs(r[-1])  # 最后一列是 rank
            rank_norm = 1.0 - min(1.0, rank_val / max_rank)  # 0~1，越高越匹配
            score = atom.importance * 0.6 + rank_norm * 0.4
            scored.append((score, atom))

        scored.sort(key=lambda x: -x[0])
        return [sa[1] for sa in scored[:k]]

    async def get_by_user(self, user_id: str, status: str | None = "active") -> list[MemoryAtom]:
        """获取用户所有原子"""
        if status:
            rows = await self.fetch(
                "SELECT * FROM memory_atoms WHERE user_id = ? AND status = ? ORDER BY created_at DESC",
                (user_id, status),
            )
        else:
            rows = await self.fetch(
                "SELECT * FROM memory_atoms WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
            )
        return [self._row_to_atom(r) for r in rows]

    async def get_by_id(self, atom_id: int) -> MemoryAtom | None:
        """按 ID 获取"""
        rows = await self.fetch("SELECT * FROM memory_atoms WHERE id = ?", (atom_id,))
        return self._row_to_atom(rows[0]) if rows else None

    async def get_stats(self, user_id: str) -> dict[str, Any]:
        """记忆统计"""
        total = (await self.fetchone(
            "SELECT COUNT(*) FROM memory_atoms WHERE user_id = ?", (user_id,)
        ))[0]
        by_type = await self.fetch("""
            SELECT atom_type, COUNT(*) FROM memory_atoms
            WHERE user_id = ? AND status = 'active'
            GROUP BY atom_type
        """, (user_id,))
        return {"total": total, "by_type": dict(by_type)}

    async def get_all_active_user_ids(self) -> list[str]:
        """获取所有有活跃原子的用户"""
        rows = await self.fetch(
            "SELECT DISTINCT user_id FROM memory_atoms WHERE status = 'active'"
        )
        return [r[0] for r in rows]

    async def get_timeline(
        self, user_id: str, page: int = 1, page_size: int = 20
    ) -> dict:
        """按日期分组的时间线"""
        rows = await self.fetch("""
            SELECT diary_date, COUNT(*) as cnt,
                   ROUND(AVG(importance), 2) as avg_imp,
                   SUM(access_count) as total_access
            FROM memory_atoms WHERE user_id=? AND status='active'
            GROUP BY diary_date ORDER BY diary_date DESC
            LIMIT ? OFFSET ?
        """, (user_id, page_size, (page - 1) * page_size))

        total = (await self.fetchone(
            "SELECT COUNT(DISTINCT diary_date) FROM memory_atoms WHERE user_id=? AND status='active'",
            (user_id,),
        ))[0]

        result = []
        for d in rows:
            date_str, cnt, avg_imp, acc = d
            atoms = await self.fetch("""
                SELECT id, content, atom_type, importance, diary_snippet
                FROM memory_atoms WHERE user_id=? AND diary_date=? AND status='active'
                ORDER BY importance DESC LIMIT 3
            """, (user_id, date_str))
            result.append({
                "date": date_str,
                "atom_count": cnt,
                "avg_importance": avg_imp,
                "total_access": acc or 0,
                "atoms_preview": [
                    {"id": a[0], "content": a[1][:100], "type": a[2], "importance": a[3], "snippet": a[4] or ""}
                    for a in atoms
                ],
            })

        return {"total": total, "page": page, "page_size": page_size, "items": result}

    async def get_day_atoms(self, user_id: str, date: str) -> list[dict]:
        """获取某天的所有原子（page_api 用）"""
        rows = await self.fetch("""
            SELECT id, content, atom_type, importance, confidence, access_count,
                   diary_snippet, entities, status, created_at
            FROM memory_atoms WHERE user_id=? AND diary_date=? AND status='active'
            ORDER BY importance DESC
        """, (user_id, date))
        return [
            {"id": r[0], "content": r[1], "type": r[2], "importance": r[3],
             "confidence": r[4], "access_count": r[5], "snippet": r[6] or "",
             "entities": json.loads(r[7]) if isinstance(r[7], str) and r[7] else [],
             "status": r[8]}
            for r in rows
        ]

    # ═══════════════════════════════════════════════════
    #  更新
    # ═══════════════════════════════════════════════════

    async def touch(self, atom_id: int):
        """更新访问时间和计数"""
        await self.execute(
            "UPDATE memory_atoms SET last_accessed_at=?, access_count=access_count+1 WHERE id=?",
            (time.time(), atom_id),
        )

    async def delete(self, atom_id: int, user_id: str) -> bool:
        """软删除"""
        c = await self.execute(
            "UPDATE memory_atoms SET status='forgotten' WHERE id=? AND user_id=?",
            (atom_id, user_id),
        )
        return c.rowcount > 0

    async def update_atom(self, atom_id: int, **fields) -> bool:
        """更新原子字段"""
        allowed = {"content", "atom_type", "importance", "status"}
        sets = []
        vals = []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k} = ?")
                vals.append(v)
        if not sets:
            return False
        vals.append(atom_id)
        c = await self.execute(
            f"UPDATE memory_atoms SET {', '.join(sets)} WHERE id = ?", vals
        )
        return c.rowcount > 0

    async def apply_decay(self, decay_rate: float = 0.99):
        """全局重要性衰减"""
        await self.execute(
            "UPDATE memory_atoms SET importance = importance * ? WHERE status = 'active'",
            (decay_rate,),
        )

    # ═══════════════════════════════════════════════════
    #  全局事实（去重架构 v4）
    # ═══════════════════════════════════════════════════

    async def ensure_fact(self, content: str, atom_type: str = "unknown",
                          importance: float = 0.5, confidence: float = 0.7) -> int:
        """插入或查找全局事实，返回 fact_id"""
        import time
        now = time.time()
        async with self._connect() as db:
            row = await db.execute_fetchall(
                "SELECT id, source_count FROM atomic_facts WHERE content = ?",
                (content,),
            )
            if row:
                fact_id, count = row[0]
                new_imp = min(1.0, importance + 0.05)
                await db.execute(
                    "UPDATE atomic_facts SET importance=?, source_count=source_count+1, updated_at=? WHERE id=?",
                    (new_imp, now, fact_id),
                )
            else:
                cursor = await db.execute(
                    "INSERT INTO atomic_facts (content, atom_type, importance, confidence, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                    (content, atom_type, importance, confidence, now, now),
                )
                fact_id = cursor.lastrowid
            await db.commit()
        return fact_id

    async def link_fact(self, diary_id: int, fact_id: int, importance: float = 0.5, snippet: str = ""):
        """关联日记 → 事实"""
        async with self._connect() as db:
            await db.execute(
                "INSERT OR IGNORE INTO diary_fact_links (diary_id, fact_id, importance, snippet) VALUES (?,?,?,?)",
                (diary_id, fact_id, importance, snippet),
            )
            await db.commit()

    async def get_facts_by_diary(self, diary_id: int) -> list[dict]:
        """查询日记关联的所有事实"""
        rows = await self.fetch("""
            SELECT af.id, af.content, af.atom_type, af.importance, af.confidence,
                   dfl.importance as link_imp, dfl.snippet
            FROM atomic_facts af
            JOIN diary_fact_links dfl ON af.id = dfl.fact_id
            WHERE dfl.diary_id = ?
            ORDER BY dfl.importance DESC
        """, (diary_id,))
        return [
            {"id": r[0], "content": r[1], "type": r[2],
             "importance": r[3], "confidence": r[4],
             "link_importance": r[5], "snippet": r[6] or ""}
            for r in rows
        ]

    # ═══════════════════════════════════════════════════
    #  内部工具
    # ═══════════════════════════════════════════════════

    COLUMNS = (
        "id", "user_id", "diary_date", "atom_type", "content", "entities",
        "importance", "confidence", "access_count", "created_at", "last_accessed_at",
        "ttl_days", "expires_at", "decay_type", "status", "session_id", "diary_ref",
        "diary_snippet", "embedding", "embedding_model", "metadata", "diary_id",
    )

    def _row_to_atom(self, row) -> MemoryAtom:
        """数据库行 → MemoryAtom"""
        d = dict(zip(self.COLUMNS, row))
        return MemoryAtom(
            atom_id=d["id"],
            user_id=d["user_id"],
            diary_date=d["diary_date"],
            atom_type=AtomType(d["atom_type"]),
            content=d["content"],
            entities=json.loads(d["entities"]) if isinstance(d["entities"], str) else (d["entities"] or []),
            importance=d["importance"],
            confidence=d["confidence"],
            access_count=d["access_count"],
            created_at=d["created_at"],
            last_accessed_at=d["last_accessed_at"],
            ttl_days=d["ttl_days"],
            status=AtomStatus(d["status"]),
            session_id=d["session_id"],
            diary_ref=d["diary_ref"],
            expires_at=d["expires_at"] or 0.0,
            decay_type=self._parse_decay_type(d["decay_type"]),
            diary_snippet=d["diary_snippet"] or "",
            metadata=json.loads(d["metadata"]) if isinstance(d["metadata"], str) and d["metadata"] else {},
        )

    @staticmethod
    def _atom_values(atom: MemoryAtom) -> tuple:
        """MemoryAtom → INSERT VALUES tuple"""
        return (
            atom.user_id, atom.diary_date, atom.atom_type.value, atom.content,
            json.dumps(atom.entities, ensure_ascii=False),
            atom.importance, atom.confidence, atom.access_count,
            atom.created_at, atom.last_accessed_at, atom.ttl_days,
            atom.expires_at, atom.decay_type.value, atom.status.value,
            atom.session_id, atom.diary_ref,
            atom.diary_snippet,
            json.dumps(atom.metadata, ensure_ascii=False),
            atom.diary_id,
        )

    def _sanitize_fts_query(self, query: str) -> str:
        """清理 FTS5 查询字符串"""
        if not query or not query.strip():
            return ""
        import re
        cleaned = re.sub(r'[^\w一-鿿]', ' ', query).strip()
        if not cleaned:
            return ""
        terms = cleaned.split()
        return ' AND '.join(f'"{t}"*' for t in terms if t)

    def _parse_decay_type(self, raw):
        if not raw:
            return DecayType.EXPONENTIAL
        try:
            return DecayType(raw)
        except (ValueError, TypeError):
            for dt in DecayType:
                if dt.value in str(raw).lower():
                    return dt
            return DecayType.EXPONENTIAL
