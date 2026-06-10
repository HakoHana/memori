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
# 迁移方法名必须为 _migrate_v{version}。
CURRENT_VERSION = 3

# ── 迁移清单（用于日志和调试） ────────────────────────────
MIGRATION_MANIFEST: dict[int, dict[str, Any]] = {
    1: {
        "description": "初始扩展：diary_entries 补充列 + memory_atoms 补充列 + diary_id",
        "type": "schema",
        "requires_backup": False,
        "tables_affected": ["diary_entries", "memory_atoms"],
    },
    2: {
        "description": "会话存储：sessions + messages 表",
        "type": "schema",
        "requires_backup": False,
        "tables_affected": ["sessions", "messages"],
    },
    3: {
        "description": "schema 整合：移除所有 inline ALTER TABLE 逻辑，将 atom_store 和 diary_store 中离散的列补齐迁移收归此处统一管理。后续所有 schema 变更只在此文件添加。",
        "type": "schema",
        "requires_backup": True,
        "tables_affected": ["memory_atoms", "diary_entries", "graph_nodes", "graph_edges"],
    },
}


class DBMigration(BaseDbStore):
    """管理数据库 schema 版本迁移"""

    # 是否创建备份（可被测试覆写）
    create_backup: bool = True

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
        if current >= CURRENT_VERSION:
            logger.info(f"[Migration] schema 已是最新 (v{current})")
            return []

        applied: list[int] = []
        for version in range(current + 1, CURRENT_VERSION + 1):
            manifest = MIGRATION_MANIFEST.get(version, {})
            desc = manifest.get("description", f"v{version}")

            # 需要备份？
            if manifest.get("requires_backup") and self.create_backup:
                backup_path = await self._backup(version)
                if backup_path:
                    logger.info(f"[Migration] v{version} 备份已创建: {backup_path}")

            if dry_run:
                logger.info(f"[Migration] [DRY-RUN] v{version}: {desc}")
                applied.append(version)
                continue

            # 执行迁移
            method_name = f"_migrate_v{version}"
            method = getattr(self, method_name, None)
            if not method:
                logger.warning(f"[Migration] v{version}: 未找到 {method_name}，跳过")
                continue

            try:
                await method()
                await self._record_migration(version, desc)
                logger.info(f"[Migration] v{version} 完成: {desc}")
                applied.append(version)
            except Exception as e:
                logger.error(f"[Migration] v{version} 失败: {e}")
                raise RuntimeError(f"Migration v{version} failed: {e}") from e

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
        """检查当前 schema 完整性，返回每张表的列状态"""
        expected_columns = {
            "memory_atoms": [
                "id", "user_id", "diary_date", "atom_type", "content",
                "entities", "importance", "confidence", "access_count",
                "created_at", "last_accessed_at", "ttl_days", "expires_at",
                "decay_type", "status", "session_id", "diary_ref",
                "diary_snippet", "embedding", "embedding_model", "metadata",
            ],
            "diary_entries": [
                "id", "user_id", "date", "content", "topics", "sentiment",
                "importance", "atom_count", "created_at", "updated_at", "status",
            ],
            "consolidation_state": [
                "user_id", "msg_count", "warmup_threshold", "last_consolidated_at",
                "last_diary_date", "diary_count", "diary_count_since_persona",
                "l1_retry_count",
            ],
            "sessions": [
                "session_id", "user_id", "platform", "created_at",
                "last_active_at", "message_count", "metadata",
            ],
            "messages": [
                "id", "session_id", "role", "content", "timestamp", "metadata",
            ],
            "graph_nodes": [
                "id", "node_key", "node_type", "value", "canonical_value",
                "metadata", "created_at", "updated_at",
            ],
            "graph_edges": [
                "id", "edge_key", "semantic_key", "source_node_id",
                "target_node_id", "relation_type", "source_memory_id",
                "weight", "confidence", "status", "metadata",
                "created_at", "updated_at",
            ],
        }

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
        """v1: 补充 diary_entries 和 memory_atoms 的列"""
        async with self._connect() as db:
            # diary_entries 补充列
            for col in ["topics", "sentiment", "importance", "atom_count", "status"]:
                try:
                    await db.execute(f"ALTER TABLE diary_entries ADD COLUMN {col}")
                except Exception:
                    pass

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

    async def _migrate_v2(self):
        """v2: 创建 sessions 和 messages 表"""
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
                    timestamp REAL NOT NULL,
                    metadata TEXT DEFAULT '{}'
                )
            """)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_sid ON messages(session_id, id DESC)"
            )
            await db.commit()

    async def _migrate_v3(self):
        """
        v3: schema 整合 — 确保所有历史补丁列存在

        此版本将之前分散在各 Store.initialize() 中的
        inline ALTER TABLE 全部收归此处。只要 v3 跑过，
        所有 store 初始化时可安全假定列齐全。
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
                # 检查列是否已存在
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

            # ── diary_entries 完整列检查 ──
            for col_def in [
                "topics TEXT DEFAULT '[]'",
                "sentiment TEXT DEFAULT ''",
                "importance REAL DEFAULT 0.5",
                "atom_count INTEGER DEFAULT 0",
                "status TEXT DEFAULT 'active'",
            ]:
                col_name = col_def.split()[0]
                cols = await db.execute_fetchall(
                    "SELECT name FROM pragma_table_info('diary_entries') WHERE name=?",
                    (col_name,),
                )
                if not cols:
                    try:
                        await db.execute(
                            f"ALTER TABLE diary_entries ADD COLUMN {col_def}"
                        )
                        logger.info(f"[Migration] diary_entries 补充列: {col_name}")
                    except Exception as e:
                        logger.warning(
                            f"[Migration] diary_entries 补充列 {col_name} 失败: {e}"
                        )

            # ── graph_nodes 检查（确保外键模式启用） ──
            nodes_cols = await db.execute_fetchall(
                "SELECT name FROM pragma_table_info('graph_nodes')"
            )
            node_col_names = {r[0] for r in nodes_cols}
            if node_col_names:
                # 表存在，补充可能缺失的列
                for col_def in [
                    "canonical_value TEXT DEFAULT ''",
                ]:
                    col_name = col_def.split()[0]
                    if col_name not in node_col_names:
                        try:
                            await db.execute(
                                f"ALTER TABLE graph_nodes ADD COLUMN {col_def}"
                            )
                        except Exception:
                            pass

            # ── consolidation_state 表完整性 ──
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

            # ── 索引完整性 ──
            # 这些索引有些只在 atom_store.initialize() 中创建，
            # 迁移系统不保证它们存在 → 在这里统一兜底
            for idx_def in [
                "CREATE INDEX IF NOT EXISTS idx_atoms_user_status_date ON memory_atoms(user_id, status, diary_date)",
                "CREATE INDEX IF NOT EXISTS idx_atoms_user_status_imp ON memory_atoms(user_id, status, importance DESC)",
                "CREATE INDEX IF NOT EXISTS idx_atoms_user_type ON memory_atoms(user_id, atom_type)",
                "CREATE INDEX IF NOT EXISTS idx_atoms_status_ttl ON memory_atoms(status, ttl_days)",
                "CREATE INDEX IF NOT EXISTS idx_atoms_user ON memory_atoms(user_id)",
                "CREATE INDEX IF NOT EXISTS idx_diary_user_date ON diary_entries(user_id, date)",
                "CREATE INDEX IF NOT EXISTS idx_graph_nodes_type ON graph_nodes(node_type, canonical_value)",
                "CREATE INDEX IF NOT EXISTS idx_graph_edges_semantic ON graph_edges(semantic_key)",
                "CREATE INDEX IF NOT EXISTS idx_graph_edges_memory ON graph_edges(source_memory_id)",
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
