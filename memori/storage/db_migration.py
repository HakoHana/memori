"""版本化数据库迁移 — 集中式 schema 变更管理

架构对比参考（file:///tmp/architecture-comparison.html → Candidate 4）：
- 旧方案：零散 ALTER TABLE + try/except 散布在各 Store.initialize() 中
- 新方案：全量 schema 变更集中在此，版本号追踪，迁移前自动备份

每版迁移必须：
1. 是幂等的（可重复执行，用 IF NOT EXISTS / try-ALTER 保护）
2. 有对应版本号的 _migrate_vN 方法
3. 更新 CURRENT_VERSION
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path
from typing import Any

from ..core.logger import logger

from .base_store import BaseDbStore

# ── 当前 schema 版本 ──────────────────────────────────────
# 升级此值表示有一套新的迁移要跑。
# 迁移方法名必须为 _migrate_v{version} 或 _migrate_{scope}_v{version}。
CURRENT_VERSION = 6

# ── 每类数据库的 schema 版本（默认 0 表示由 Store.initialize() 统一建表） ──
VERSIONS: dict[str, int] = {
    "memory": 3,         # v1→v3：memory_atoms 列补齐 + 索引；v2 已迁至 conversations.db
    "diaries": 0,        # 由 DiaryStore.initialize() 统一建表
    "conversations": 0,  # 由 ConversationStore.initialize() 统一建表
    "graph": 1,          # v1：ISO 字符串 → epoch float 统一
    "state": 0,          # 由 StateStore.initialize() 统一建表
}

# ── 迁移清单（用于日志和调试） ────────────────────────────
MIGRATION_MANIFEST: dict[int, dict[str, Any]] = {
    1: {
        "description": "memory_atoms 补充列 + diary_id",
        "type": "schema",
        "requires_backup": False,
        "tables_affected": ["memory_atoms"],
    },
    2: {
        "description": "[已迁至 conversations.db] 原本创建 sessions + messages 表",
        "type": "schema",
        "requires_backup": False,
        "tables_affected": [],
    },
    3: {
        "description": "schema 整合：memory_atoms 列兜底 + 索引",
        "type": "schema",
        "requires_backup": True,
        "tables_affected": ["memory_atoms"],
    },
    6: {
        "description": "user_persona.persona_embedding 列（画像相似检测用）",
        "type": "schema",
        "requires_backup": False,
        "tables_affected": ["user_persona"],
    },
}


class DBMigration(BaseDbStore):
    """管理数据库 schema 版本迁移

    支持按 scope 区分不同数据库的迁移版本：
    - memory: 主数据库（memory_atoms、user_persona、write_ops 等）
    - diaries: 日记数据库（diary_entries、diary_fts）
    - conversations: 会话数据库（sessions、messages）
    - graph: 图谱数据库（graph_nodes、graph_edges、entity_cooccur）
    - state: 状态数据库（consolidation_state）
    """

    # 是否创建备份（可被测试覆写）
    create_backup: bool = True

    def __init__(self, db_path: str, scope: str = "memory"):
        super().__init__(db_path)
        self.scope = scope

    @property
    def current_version(self) -> int:
        """返回当前 scope 的目标版本号"""
        return VERSIONS.get(self.scope, CURRENT_VERSION)

    async def initialize(self):
        """初始化版本跟踪表"""
        async with self._connect() as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS db_version (
                    version INTEGER PRIMARY KEY,
                    migrated_at TEXT NOT NULL,
                    description TEXT,
                    checksum TEXT DEFAULT ''
                )
            """)
            await db.commit()

    # ═══════════════════════════════════════════════════
    #  版本查询
    # ═══════════════════════════════════════════════════

    async def get_current_version(self) -> int:
        """读取已迁移到的最高版本号"""
        rows = await self.fetch("SELECT COALESCE(MAX(version), 0) FROM db_version")
        return rows[0][0] if rows else 0

    async def get_migration_log(self) -> list[dict]:
        """获取完整迁移历史"""
        rows = await self.fetch(
            "SELECT version, migrated_at, description FROM db_version ORDER BY version"
        )
        return [
            {"version": r[0], "migrated_at": r[1], "description": r[2]} for r in rows
        ]

    # ═══════════════════════════════════════════════════
    #  执行迁移
    # ═══════════════════════════════════════════════════

    async def migrate(self, dry_run: bool = False) -> list[int]:
        """
        执行所有待迁移

        Args:
            dry_run: True 则只打印不执行

        Returns:
            已执行的版本号列表
        """
        current = await self.get_current_version()
        target = self.current_version
        if current >= target:
            scope_label = f"({self.scope}) " if self.scope != "memory" else ""
            logger.info(f"[Migration] {scope_label}schema 已是最新 v{current}")
            return []

        applied: list[int] = []
        for version in range(current + 1, target + 1):
            manifest = MIGRATION_MANIFEST.get(version, {})
            desc = manifest.get("description", f"v{version}")

            # 需要备份？只对 memory scope 执行
            if self.scope == "memory" and manifest.get("requires_backup") and self.create_backup:
                backup_path = await self._backup(version)
                if backup_path:
                    logger.info(f"[Migration] v{version} 备份已创建: {backup_path}")

            if dry_run:
                logger.info(f"[Migration] [DRY-RUN] v{version}: {desc}")
                applied.append(version)
                continue

            # 执行迁移：优先 scope 专属方法，回退到通用方法
            method = getattr(self, f"_migrate_{self.scope}_v{version}", None)
            if not method:
                method = getattr(self, f"_migrate_v{version}", None)
            if not method:
                logger.warning(f"[Migration] ({self.scope}) v{version}: 未找到迁移方法，跳过")
                continue

            try:
                await method()
                await self._record_migration(version, desc)
                scope_label = f"({self.scope}) " if self.scope != "memory" else ""
                logger.info(f"[Migration] {scope_label}v{version} 完成: {desc}")
                applied.append(version)
            except Exception as e:
                logger.error(f"[Migration] ({self.scope}) v{version} 失败: {e}")
                raise RuntimeError(f"Migration ({self.scope}) v{version} failed: {e}") from e

        return applied

    async def _record_migration(self, version: int, description: str):
        """在 db_version 表中记录迁移"""
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        async with self._connect() as db:
            await db.execute(
                "INSERT OR IGNORE INTO db_version(version, migrated_at, description) VALUES (?, ?, ?)",
                (version, now, description[:200]),
            )
            await db.commit()

    # ═══════════════════════════════════════════════════
    #  备份
    # ═══════════════════════════════════════════════════

    async def _backup(self, version: int) -> str | None:
        """迁移前备份 db 文件"""
        src = Path(self.db_path)
        if not src.exists():
            return None

        backup_dir = src.parent / "migration_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        backup_path = str(backup_dir / f"before_v{version}_{timestamp}.db")

        try:
            # 确保所有未完成事务都提交了
            await self.commit()
        except Exception:
            pass

        try:
            shutil.copy2(str(src), backup_path)
            return backup_path
        except Exception as e:
            logger.warning(f"[Migration] 备份失败 (v{version}): {e}")
            return None

    # ═══════════════════════════════════════════════════
    #  校验
    # ═══════════════════════════════════════════════════

    async def verify_schema(self) -> dict[str, Any]:
        """检查当前 schema 完整性，返回每张表的列状态（按 scope 只检查归属本库的表）"""
        by_scope = {
            "memory": {
                "memory_atoms": [
                    "id", "user_id", "diary_date", "atom_type", "content",
                    "entities", "importance", "confidence", "access_count",
                    "created_at", "last_accessed_at", "ttl_days", "expires_at",
                    "decay_type", "status", "session_id", "diary_ref",
                    "diary_snippet", "embedding", "embedding_model", "metadata",
                ],
                "user_persona": [
                    "uid", "summary", "full_markdown", "known_ids", "primary_name",
                    "identity_confidence", "tier", "tags", "version", "last_full_update",
                    "last_incremental_update", "incremental_count",
                    "diary_count_since_full",
                ],
                "write_ops": [
                    "id", "user_id", "action", "table_name", "record_id",
                    "payload", "status", "created_at", "updated_at",
                ],
            },
            "diaries": {
                "diary_entries": [
                    "id", "user_id", "date", "content", "topics", "sentiment",
                    "importance", "atom_count", "created_at", "updated_at", "status",
                ],
            },
            "conversations": {
                "sessions": [
                    "session_id", "user_id", "platform", "created_at",
                    "last_active_at", "message_count", "metadata",
                ],
                "messages": [
                    "id", "session_id", "role", "content", "timestamp", "metadata",
                ],
            },
            "graph": {
                "nodes": [
                    "id", "name", "type", "metadata",
                    "embedding", "embedding_model", "created_at", "updated_at",
                ],
                "edges": [
                    "id", "from_node", "to_node", "relation_type", "diary_id",
                    "weight", "confidence", "status", "metadata",
                    "created_at", "updated_at",
                ],
            },
            "state": {
                "consolidation_state": [
                    "user_id", "msg_count", "warmup_threshold", "last_consolidated_at",
                    "last_diary_date", "diary_count", "diary_count_since_persona",
                    "l1_retry_count",
                ],
            },
        }
        expected_columns = by_scope.get(self.scope, {})

        result = {}
        for table, cols in expected_columns.items():
            table_info = {"exists": False, "missing": cols.copy(), "extra": []}
            try:
                rows = await self.fetch(f"PRAGMA table_info({table})")
                if rows:
                    table_info["exists"] = True
                    existing = {r[1] for r in rows}  # r[1] = column name
                    table_info["missing"] = [c for c in cols if c not in existing]
                    table_info["extra"] = [r[1] for r in rows if r[1] not in cols]
            except Exception:
                pass
            result[table] = table_info

        # 汇总
        all_ok = all(
            info["exists"] and not info["missing"] for info in result.values()
        )
        result["_summary"] = {
            "all_tables_exist": all(info["exists"] for info in result.values()),
            "all_columns_present": all_ok,
        }
        return result

    # ═══════════════════════════════════════════════════
    #  迁移实现
    # ═══════════════════════════════════════════════════

    async def _migrate_v1(self):
        """v1: memory_atoms 补充列 + diary_id（diary_entries 部分已迁至 diaries.db）"""
        async with self._connect() as db:
            # memory_atoms 补充列
            for col in ["diary_snippet", "expires_at", "decay_type"]:
                try:
                    await db.execute(f"ALTER TABLE memory_atoms ADD COLUMN {col}")
                except Exception:
                    pass

            try:
                await db.execute("ALTER TABLE memory_atoms ADD COLUMN diary_id INTEGER DEFAULT 0")
            except Exception:
                pass

            await db.commit()

    async def _migrate_v3(self):
        """
        v3: memory_atoms 列兜底 + 索引覆盖

        只检查 memory.db 中仍保留的表（memory_atoms、write_ops）。
        diary_entries / graph_nodes / consolidation_state 等表
        已迁至独立的 diaries.db / graph.db / state.db。
        """
        async with self._connect() as db:
            # ── memory_atoms 完整列检查 ──
            for col_def in [
                "diary_snippet TEXT DEFAULT ''",
                "expires_at REAL NOT NULL DEFAULT 0.0",
                "decay_type TEXT NOT NULL DEFAULT 'exponential'",
                "diary_id INTEGER DEFAULT 0",
            ]:
                col_name = col_def.split()[0]
                cols = await db.execute_fetchall(
                    "SELECT name FROM pragma_table_info('memory_atoms') WHERE name=?",
                    (col_name,),
                )
                if not cols:
                    try:
                        await db.execute(
                            f"ALTER TABLE memory_atoms ADD COLUMN {col_def}"
                        )
                        logger.info(f"[Migration] memory_atoms 补充列: {col_name}")
                    except Exception as e:
                        logger.warning(
                            f"[Migration] memory_atoms 补充列 {col_name} 失败: {e}"
                        )

            # ── 索引完整性（仅限 memory.db 中的表） ──
            for idx_def in [
                "CREATE INDEX IF NOT EXISTS idx_atoms_user_status_date ON memory_atoms(user_id, status, diary_date)",
                "CREATE INDEX IF NOT EXISTS idx_atoms_user_status_imp ON memory_atoms(user_id, status, importance DESC)",
                "CREATE INDEX IF NOT EXISTS idx_atoms_user_type ON memory_atoms(user_id, atom_type)",
                "CREATE INDEX IF NOT EXISTS idx_atoms_status_ttl ON memory_atoms(status, ttl_days)",
                "CREATE INDEX IF NOT EXISTS idx_atoms_user ON memory_atoms(user_id)",
                "CREATE INDEX IF NOT EXISTS idx_write_ops_status ON write_ops(status, updated_at)",
            ]:
                try:
                    await db.execute(idx_def)
                except Exception:
                    pass

            await db.commit()

        # 迁移后验证
        try:
            verification = await self.verify_schema()
            summary = verification.get("_summary", {})
            if not summary.get("all_tables_exist"):
                missing_tables = [
                    t for t, info in verification.items()
                    if t != "_summary" and not info.get("exists")
                ]
                logger.warning(
                    f"[Migration] v3 后仍有表缺失: {missing_tables}"
                )
            if not summary.get("all_columns_present"):
                total_missing = sum(
                    len(info.get("missing", []))
                    for t, info in verification.items()
                    if t != "_summary"
                )
                if total_missing:
                    logger.warning(
                        f"[Migration] v3 后仍有 {total_missing} 个列未补齐"
                    )
            logger.info(f"[Migration] v3 schema 校验完成: {verification['_summary']}")
        except Exception as e:
            logger.warning(f"[Migration] v3 schema 校验异常: {e}")

    async def _migrate_v4(self):
        """
        v4: 删除冗余的 atomic_facts 和 diary_fact_links 表

        功能已由 memory_atoms 完整覆盖，不再需要独立的全局事实表。
        DROP 前不做备份，数据已在 memory_atoms 中有完整副本。
        """
        async with self._connect() as db:
            for tbl in ("diary_fact_links", "atomic_facts"):
                try:
                    await db.execute(f"DROP TABLE IF EXISTS {tbl}")
                except Exception:
                    pass
            await db.commit()
        logger.info("[Migration] v4 完成: 已删除 atomic_facts / diary_fact_links")

    async def _migrate_v5(self):
        """
        v5: 创建 atoms_diary_links 桥表（原子↔日记多对多），从旧 diary_id 迁移数据

        memory_atoms.diary_id 仍保留但不作为主关联路径，
        新的多对多关联通过 atoms_diary_links 桥表实现。
        """
        async with self._connect() as db:
            # 创建桥表
            await db.execute("""
                CREATE TABLE IF NOT EXISTS atoms_diary_links (
                    atom_id INTEGER NOT NULL,
                    diary_id INTEGER NOT NULL,
                    snippet TEXT DEFAULT '',
                    importance REAL DEFAULT 0.5,
                    PRIMARY KEY (atom_id, diary_id)
                )
            """)
            try:
                await db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_adl_atom ON atoms_diary_links(atom_id)"
                )
            except Exception:
                pass
            try:
                await db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_adl_diary ON atoms_diary_links(diary_id)"
                )
            except Exception:
                pass
            # 从旧 diary_id 迁移数据
            await db.execute("""
                INSERT OR IGNORE INTO atoms_diary_links (atom_id, diary_id, snippet, importance)
                SELECT id, diary_id, COALESCE(diary_snippet, ''), importance
                FROM memory_atoms WHERE diary_id > 0
            """)
            await db.commit()
        logger.info("[Migration] v5 完成: 桥表 atoms_diary_links 已就绪")

    async def _migrate_v6(self):
        """v6: user_persona.persona_embedding 列（画像相似检测用）"""
        async with self._connect() as db:
            try:
                await db.execute(
                    "ALTER TABLE user_persona ADD COLUMN persona_embedding BLOB DEFAULT NULL"
                )
            except Exception:
                pass
            try:
                await db.execute(
                    "ALTER TABLE user_persona ADD COLUMN embedding_model TEXT DEFAULT ''"
                )
            except Exception:
                pass
            await db.commit()
        logger.info("[Migration] v6 完成: persona_embedding 列已就绪")
        """v1: 图谱时间戳从 ISO 8601 字符串统一为 epoch float"""
        from datetime import datetime

        for table in ("nodes", "edges"):
            try:
                rows = await self.fetch(
                    f"SELECT rowid, created_at, updated_at FROM {table} "
                    "WHERE created_at LIKE '2%' OR created_at LIKE '1%'"
                )
                for row in rows:
                    rid, ca, ua = row
                    try:
                        new_ca = str(datetime.fromisoformat(ca.replace("Z", "+00:00")).timestamp())
                        new_ua = str(datetime.fromisoformat(ua.replace("Z", "+00:00")).timestamp())
                        await self.execute(
                            f"UPDATE {table} SET created_at=?, updated_at=? WHERE rowid=?",
                            (new_ca, new_ua, rid),
                        )
                    except Exception:
                        pass
            except Exception:
                pass
        logger.info("[Migration] graph v1 完成: 已转换 ISO 时间戳 → epoch float")
