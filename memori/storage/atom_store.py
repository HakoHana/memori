"""SQLite 原子存储 — 实现 MemoryStore 接口"""

from __future__ import annotations

import json
import time
from typing import Any

from ..models.memory_atom import MemoryAtom, AtomType, AtomStatus, DecayType
from ..core.adapters import MemoryStore
from ..core.logger import logger
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


class AtomStore(BaseDbStore, MemoryStore):
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

            # 原子 ↔ 日记 多对多桥表
            await db.execute("""
                CREATE TABLE IF NOT EXISTS atoms_diary_links (
                    atom_id INTEGER NOT NULL,
                    diary_id INTEGER NOT NULL,
                    snippet TEXT DEFAULT '',
                    importance REAL DEFAULT 0.5,
                    PRIMARY KEY (atom_id, diary_id)
                )
            """)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_adl_atom ON atoms_diary_links(atom_id)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_adl_diary ON atoms_diary_links(diary_id)"
            )

            # 用户注册表（已废弃，由 canonical_users + user_identities 替代）
            # user_registry 表在 db_migration.py v9 中删除

            # 规范用户 ID（身份体系 v2）
            await db.execute("""
                CREATE TABLE IF NOT EXISTS canonical_users (
                    uid TEXT PRIMARY KEY,
                    primary_name TEXT,
                    identity_confidence REAL DEFAULT 0.3,
                    created_at REAL,
                    updated_at REAL
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS user_identities (
                    platform_id TEXT PRIMARY KEY,
                    uid TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    display_name TEXT,
                    first_seen REAL,
                    last_seen REAL,
                    verified INTEGER DEFAULT 0,
                    source TEXT DEFAULT 'auto'
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_identity_uid ON user_identities(uid)
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS user_persona (
                    uid TEXT PRIMARY KEY,
                    summary TEXT,
                    full_markdown TEXT,
                    known_ids TEXT DEFAULT '[]',
                    primary_name TEXT,
                    identity_confidence REAL DEFAULT 0.3,
                    tier TEXT DEFAULT 'new',
                    tags TEXT DEFAULT '[]',
                    version INTEGER DEFAULT 1,
                    last_full_update REAL,
                    last_incremental_update REAL,
                    incremental_count INTEGER DEFAULT 0,
                    diary_count_since_full INTEGER DEFAULT 0,
                    created_at REAL,
                    updated_at REAL
                )
            """)

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

    async def search_fts(self, query: str, user_id: str | None = None, k: int = 5,
                          imp_weight: float = 0.6, rank_weight: float = 0.4,
                          extra_user_ids: list[str] | None = None) -> list[MemoryAtom]:
        """FTS5 全文搜索（按重要度 × BM25 排序）

        Args:
            user_id: 用户 ID，None 表示搜索所有用户（全局去重用）
            extra_user_ids: 额外搜索的用户 ID，用于搜索关联身份的记忆
        """
        safe_query = self._sanitize_fts_query(query)
        if not safe_query:
            return []

        # 构建用户 ID 条件
        if user_id is None:
            # 全局搜索（不按用户过滤）
            candidates = k * 3
            sql = """
                SELECT a.*, rank FROM memory_atoms a
                JOIN memory_atoms_fts f ON a.id = f.atom_id
                WHERE memory_atoms_fts MATCH ? AND a.status = 'active'
                ORDER BY rank
                LIMIT ?
            """
            rows = await self.fetch(sql, (safe_query, candidates))
        else:
            all_uids = [user_id]
            if extra_user_ids:
                for eid in extra_user_ids:
                    if eid and eid != user_id and eid not in all_uids:
                        all_uids.append(eid)
            placeholders = ",".join("?" for _ in all_uids)
            candidates = k * 3
            sql = f"""
                SELECT a.*, rank FROM memory_atoms a
                JOIN memory_atoms_fts f ON a.id = f.atom_id
                WHERE memory_atoms_fts MATCH ? AND a.user_id IN ({placeholders}) AND a.status = 'active'
                ORDER BY rank
                LIMIT ?
            """
            rows = await self.fetch(sql, (safe_query, *all_uids, candidates))

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
            score = atom.importance * imp_weight + rank_norm * rank_weight
            scored.append((score, atom))

        scored.sort(key=lambda x: -x[0])
        return [sa[1] for sa in scored[:k]]

    # ═══════════════════════════════════════════════════
    #  向量搜索
    # ═══════════════════════════════════════════════════

    async def update_embedding(self, atom_id: int, embedding: list[float], model_name: str):
        """写入单条原子的 embedding 向量

        Args:
            atom_id: 原子 ID
            embedding: 浮点数向量
            model_name: 嵌入模型名称（用于溯源）
        """
        import json
        blob = json.dumps(embedding).encode("utf-8")
        await self.execute(
            "UPDATE memory_atoms SET embedding=?, embedding_model=? WHERE id=?",
            (blob, model_name, atom_id),
        )

    async def search_vector(
        self,
        query_embed: list[float],
        user_id: str | None = None,
        k: int = 5,
        model_name: str = "",
    ) -> list[MemoryAtom]:
        """余弦相似度向量搜索

        Python 级计算（SQLite 无原生向量索引），适合数千条级别数据。
        加载有 embedding 的活跃原子 → 逐条余弦相似度 → 排序取 top-k。

        Args:
            query_embed: 查询向量
            user_id: 用户 ID，None = 全库搜索
            k: 返回 top N
            model_name: 过滤指定模型，空字符串则不限

        Returns:
            按相似度降序排列的 MemoryAtom 列表
        """
        if not query_embed:
            return []

        model_filter = "AND embedding_model=?" if model_name else ""
        params: list = []
        if model_name:
            params.append(model_name)

        if user_id:
            rows = await self.fetch(
                f"SELECT * FROM memory_atoms WHERE user_id=? AND status='active' "
                f"AND embedding IS NOT NULL {model_filter} "
                f"ORDER BY importance DESC LIMIT ?",
                (user_id, *params, k * 20),
            )
        else:
            rows = await self.fetch(
                f"SELECT * FROM memory_atoms WHERE status='active' "
                f"AND embedding IS NOT NULL {model_filter} "
                f"ORDER BY importance DESC LIMIT ?",
                (*params, k * 20),
            )

        if not rows:
            return []

        # 余弦相似度
        q_norm = sum(x * x for x in query_embed) ** 0.5
        if q_norm < 1e-10:
            return []

        scored: list[tuple[float, list]] = []
        for row in rows:
            atom = self._row_to_atom(row)
            stored = atom.embedding
            if not stored or len(stored) != len(query_embed):
                continue
            dot = sum(a * b for a, b in zip(query_embed, stored))
            s_norm = sum(x * x for x in stored) ** 0.5
            if s_norm < 1e-10:
                continue
            sim = dot / (q_norm * s_norm)
            scored.append((sim, atom))

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

    async def get_related_user_ids(self, user_id: str) -> list[str]:
        """根据任一用户 ID/昵称，找到所有关联的 user_id（弥合 QQ 号和显示名的差异）

        例如 2398604399 → 查出 ['2398604399', 'Hako']
        """
        related = [user_id]
        try:
            # 方法1：通过 user_identities 表找到 canonical uid，再查所有平台 ID
            row = await self.fetchone(
                "SELECT uid FROM user_identities WHERE platform_id=? OR platform_id=?",
                (user_id, f"qq:{user_id}"),
            )
            if row:
                cuid = row[0]
                rows = await self.fetch(
                    "SELECT platform_id FROM user_identities WHERE uid=?", (cuid,)
                )
                for r in rows:
                    pid = r[0]
                    if pid and pid not in related:
                        # 去掉 qq: 前缀
                        clean = pid.replace("qq:", "", 1) if pid.startswith("qq:") else pid
                        if clean not in related:
                            related.append(clean)
                # 同时也查 canonical_users 的 primary_name
                crow = await self.fetchone(
                    "SELECT primary_name FROM canonical_users WHERE uid=?", (cuid,)
                )
                if crow and crow[0] and crow[0] not in related:
                    related.append(crow[0])
        except Exception:
            pass

        return related

    # ── 原子 ↔ 日记 多对多桥表 ──

    async def link_atom_to_diary(self, atom_id: int, diary_id: int, snippet: str = "", importance: float = 0.5):
        """将原子关联到日记"""
        async with self._connect() as db:
            await db.execute(
                "INSERT OR IGNORE INTO atoms_diary_links (atom_id, diary_id, snippet, importance) VALUES (?,?,?,?)",
                (atom_id, diary_id, snippet or "", importance),
            )
            await db.commit()

    async def get_diaries_by_atom(self, atom_id: int) -> list[dict]:
        """查询原子关联的所有日记"""
        rows = await self.fetch("""
            SELECT d.diary_id, d.snippet, d.importance FROM atoms_diary_links d
            WHERE d.atom_id = ?
            ORDER BY d.importance DESC
        """, (atom_id,))
        return [{"diary_id": r[0], "snippet": r[1], "importance": r[2]} for r in rows]

    async def get_atoms_by_diary(self, diary_id: int, status: str = "active") -> list[dict]:
        """查询日记关联的所有原子"""
        rows = await self.fetch("""
            SELECT a.* FROM memory_atoms a
            JOIN atoms_diary_links d ON a.id = d.atom_id
            WHERE d.diary_id = ? AND a.status = ?
            ORDER BY d.importance DESC
        """, (diary_id, status))
        return [self._row_to_atom(r) for r in rows]

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
    #  身份体系（v2：canonical_users + user_identities）
    # ═══════════════════════════════════════════════════

    async def ensure_canonical_user(self, platform_id: str, display_name: str = "",
                                     platform: str = "qq") -> tuple[str, str]:
        """确保用户存在 canonical_users + user_identities，返回 (uid, name)"""
        import time
        now = time.time()
        name = display_name.strip() or platform_id

        # 查是否已有此平台 ID
        row = await self.fetchone(
            "SELECT uid FROM user_identities WHERE platform_id = ?", (platform_id,)
        )
        if row:
            uid = row[0]
            # 更新名字和最后活跃
            await self.execute(
                "UPDATE user_identities SET display_name=?, last_seen=? WHERE platform_id=?",
                (name, now, platform_id),
            )
            await self.execute(
                "UPDATE canonical_users SET primary_name=?, updated_at=? WHERE uid=?",
                (name, now, uid),
            )
            return uid, name

        # 创建新 UID
        import uuid
        uid = "u_" + uuid.uuid4().hex[:12]
        await self.execute(
            "INSERT INTO canonical_users (uid, primary_name, created_at, updated_at) VALUES (?,?,?,?)",
            (uid, name, now, now),
        )
        await self.execute(
            "INSERT INTO user_identities (platform_id, uid, platform, display_name, first_seen, last_seen, source) VALUES (?,?,?,?,?,?,?)",
            (platform_id, uid, platform, name, now, now, "auto"),
        )
        return uid, name

    async def init_bot_identity(self, bot_name: str = "Hana"):
        """初始化 bot 自己的身份（启动时调用）"""
        import time
        now = time.time()
        row = await self.fetchone(
            "SELECT uid FROM canonical_users WHERE uid='bot_hana'"
        )
        if not row:
            await self.execute(
                "INSERT INTO canonical_users (uid, primary_name, created_at, updated_at) VALUES ('bot_hana',?,?,?)",
                (bot_name, now, now),
            )
            await self.execute(
                "INSERT INTO user_identities (platform_id, uid, platform, display_name, first_seen,last_seen,verified,source) VALUES (?,?,?,?,?,?,1,'system')",
                (f"bot:{bot_name}", "bot_hana", "memori", bot_name, now, now),
            )

    # ═══════════════════════════════════════════════════
    #  身份统一查询（canonical_users + user_identities）
    # ═══════════════════════════════════════════════════

    async def resolve_identity(self, platform_id: str) -> tuple[str, str] | None:
        """platform_id → (uid, display_name)，JOIN 一次查询"""
        try:
            row = await self.fetchone("""
                SELECT i.uid, COALESCE(u.primary_name, '')
                FROM user_identities i
                JOIN canonical_users u ON i.uid = u.uid
                WHERE i.platform_id = ?
            """, (platform_id,))
            if row and row[0]:
                return (row[0], row[1])
        except Exception:
            logger.exception(f"[AtomStore] resolve_identity({platform_id}) 异常")
        return None

    async def resolve_display_name(self, uid: str) -> str:
        """uid → primary_name，查不到返回 '用户' + uid 后 4 位"""
        try:
            row = await self.fetchone(
                "SELECT primary_name FROM canonical_users WHERE uid=?", (uid,)
            )
            if row and row[0]:
                return row[0]
        except Exception:
            logger.exception(f"[AtomStore] resolve_display_name({uid}) 异常")
        return f"用户{uid[-4:]}" if len(uid) >= 4 else "用户"

    # ═══════════════════════════════════════════════════
    #  画像查询
    # ═══════════════════════════════════════════════════

    async def get_persona_summary(self, uid: str) -> str:
        """获取用户画像摘要（供注入用）"""
        row = await self.fetchone(
            "SELECT summary FROM user_persona WHERE uid=?", (uid,)
        )
        return row[0] if row else ""

    async def get_persona_tags(self, uid: str) -> list[str]:
        """获取用户标签列表"""
        row = await self.fetchone(
            "SELECT tags FROM user_persona WHERE uid=?", (uid,)
        )
        if not row or not row[0]:
            return []
        import json
        try:
            tags = json.loads(row[0])
            return [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else []
        except Exception:
            return []

    async def save_persona(self, uid: str, summary: str, full: str = "",
                            incremental: bool = False, tags: str = ""):
        """保存或更新用户画像"""
        import time
        now = time.time()
        existing = await self.fetchone(
            "SELECT uid FROM user_persona WHERE uid=?", (uid,)
        )

        if existing:
            if incremental:
                await self.execute("""
                    UPDATE user_persona SET summary=?, full_markdown=?, tags=?,
                        last_incremental_update=?, incremental_count=incremental_count+1,
                        diary_count_since_full=diary_count_since_full+1, updated_at=?
                    WHERE uid=?
                """, (summary, full, tags, now, now, uid))
            else:
                await self.execute("""
                    UPDATE user_persona SET summary=?, full_markdown=?, tags=?,
                        version=version+1, last_full_update=?, incremental_count=0,
                        diary_count_since_full=0, updated_at=?
                    WHERE uid=?
                """, (summary, full, tags, now, uid))
        else:
            await self.execute("""
                INSERT INTO user_persona (uid, summary, full_markdown, tags, version, last_full_update, created_at, updated_at)
                VALUES (?,?,?,?,1,?,?,?)
            """, (uid, summary, full, tags, now, now, now))

    async def save_persona_embedding(self, uid: str, embedding: list[float], model_name: str = ""):
        """保存画像 embedding（供相似检测用）"""
        import json
        blob = json.dumps(embedding).encode("utf-8")
        await self.execute(
            "UPDATE user_persona SET persona_embedding=?, embedding_model=?, updated_at=? WHERE uid=?",
            (blob, model_name, time.time(), uid),
        )

    async def get_all_persona_embeddings(self) -> list[dict]:
        """获取所有有 embedding 的用户画像（供梦境相似检测用）"""
        rows = await self.fetch("""
            SELECT cp.uid, cp.primary_name, up.persona_embedding, up.embedding_model,
                   up.summary, up.tags, cp.identity_confidence
            FROM canonical_users cp
            JOIN user_persona up ON cp.uid = up.uid
            WHERE up.persona_embedding IS NOT NULL AND up.persona_embedding != ''
        """)
        import json
        result = []
        for r in rows:
            try:
                emb = json.loads(r[2].decode("utf-8")) if isinstance(r[2], bytes) else json.loads(r[2])
            except (json.JSONDecodeError, Exception):
                emb = []
            result.append({
                "uid": r[0],
                "primary_name": r[1] or r[0],
                "embedding": emb,
                "embedding_model": r[3] or "",
                "summary": (r[4] or "")[:200],
                "tags": json.loads(r[5]) if isinstance(r[5], str) and r[5] else [],
                "identity_confidence": r[6] or 0.3,
            })
        return result

    # ── 通用查询（替代 routes.py 中的裸 SQL） ────────────

    async def list_users_with_persona(self) -> list[dict]:
        """用户列表（含画像数据）"""
        rows = await self.fetch("""
            SELECT cp.uid, cp.primary_name, cp.identity_confidence,
                   up.tier, up.summary, up.last_full_update
            FROM canonical_users cp
            LEFT JOIN user_persona up ON cp.uid = up.uid
            ORDER BY up.last_full_update DESC
        """)
        return [{
            "uid": r[0], "name": r[1] or r[0], "identity_confidence": r[2],
            "tier": r[3] or "new", "summary": (r[4] or "")[:100],
            "last_active": r[5],
        } for r in rows]

    async def get_user_persona(self, uid: str) -> dict | None:
        """获取用户完整画像"""
        row = await self.fetchone("SELECT * FROM user_persona WHERE uid=?", (uid,))
        if not row:
            return None
        cols = ["uid", "summary", "full_markdown", "tags", "version", "tier",
                "last_full_update", "last_incremental_update", "known_ids", "primary_name",
                "identity_confidence", "incremental_count", "diary_count_since_full",
                "created_at", "updated_at"]
        return dict(zip(cols, row))

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
            atom_type=AtomType(d["atom_type"]) if d["atom_type"] is not None else AtomType.UNKNOWN,
            content=d["content"],
            entities=json.loads(d["entities"]) if isinstance(d["entities"], str) else (d["entities"] or []),
            importance=d["importance"],
            confidence=d["confidence"],
            access_count=d["access_count"],
            created_at=d["created_at"],
            last_accessed_at=d["last_accessed_at"],
            ttl_days=d["ttl_days"],
            status=AtomStatus(d["status"]) if d["status"] is not None else AtomStatus.ACTIVE,
            session_id=d["session_id"],
            diary_ref=d["diary_ref"],
            expires_at=d["expires_at"] or 0.0,
            decay_type=self._parse_decay_type(d["decay_type"]),
            diary_snippet=d["diary_snippet"] or "",
            diary_id=d["diary_id"],
            metadata=self._safe_json_loads(d.get("metadata")),
            embedding=self._deserialize_embedding(d.get("embedding")),
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

    @staticmethod
    def _safe_json_loads(raw) -> dict:
        if not raw:
            return {}
        if not isinstance(raw, str):
            return {}
        raw = raw.strip()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except Exception:
            return {}

    @staticmethod
    def _deserialize_embedding(raw) -> list[float] | None:
        """从 BLOB 反序列化 embedding 向量"""
        if raw is None:
            return None
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            if isinstance(raw, str):
                return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            pass
        return None
