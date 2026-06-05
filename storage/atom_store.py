"""SQLite 原子存储 — 实现 MemoryStore 接口"""

from __future__ import annotations

import json
import time
from typing import Any

from ..models.memory_atom import MemoryAtom, AtomType, AtomStatus, DecayType
from .base_store import BaseDbStore


class AtomStore(BaseDbStore):
    """原子存储：SQLite + FTS5 全文搜索"""
    _pragmas = ["PRAGMA journal_mode = WAL", "PRAGMA foreign_keys = ON"]

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

    async def update_atom(self, atom_id: int, **fields) -> bool:
        """更新原子指定字段（page_api 用）"""
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
        async with self._connect() as db:
            cursor = await db.execute(
                f"UPDATE memory_atoms SET {', '.join(sets)} WHERE id = ?", vals
            )
            await db.commit()
            return cursor.rowcount > 0

    async def get_timeline(
        self, user_id: str, page: int = 1, page_size: int = 20
    ) -> dict:
        """按日期分组获取记忆时间线"""
        async with self._connect() as db:
            dates = await db.execute_fetchall("""
                SELECT diary_date, COUNT(*) as cnt,
                       ROUND(AVG(importance), 2) as avg_imp,
                       SUM(access_count) as total_access
                FROM memory_atoms WHERE user_id=? AND status='active'
                GROUP BY diary_date ORDER BY diary_date DESC
                LIMIT ? OFFSET ?
            """, (user_id, page_size, (page - 1) * page_size))

            total = (await db.execute_fetchall(
                "SELECT COUNT(DISTINCT diary_date) FROM memory_atoms WHERE user_id=? AND status='active'",
                (user_id,)
            ))[0][0]

            result = []
            for d in dates:
                date_str, cnt, avg_imp, acc = d
                # 取这条日记的前3条原子作为预览
                atoms = await db.execute_fetchall("""
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
        import json
        async with self._connect() as db:
            rows = await db.execute_fetchall("""
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

    # ── 列映射（按 SELECT * 顺序，用于 _row_to_atom） ──
    COLUMNS = (
        "id", "user_id", "diary_date", "atom_type", "content", "entities",
        "importance", "confidence", "access_count", "created_at", "last_accessed_at",
        "ttl_days", "status", "session_id", "diary_ref", "embedding",
        "embedding_model", "metadata", "diary_snippet", "expires_at", "decay_type",
    )

    def _row_dict(self, row: tuple) -> dict:
        """将数据库行转为列名字典"""
        return dict(zip(self.COLUMNS, row))

    def _row_to_atom(self, row) -> MemoryAtom:
        """数据库行 → MemoryAtom（使用列名映射）"""
        d = self._row_dict(row)
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

    def _parse_decay_type(self, raw):
        from ..models.memory_atom import DecayType
        if not raw:
            return DecayType.EXPONENTIAL
        try:
            return DecayType(raw)
        except (ValueError, TypeError):
            import re
            for dt in DecayType:
                if dt.value in str(raw).lower():
                    return dt
            return DecayType.EXPONENTIAL
