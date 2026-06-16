"""记忆核心 — 门面：统一管理所有模块（纯净版，零框架依赖）"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .logger import logger

from ..models.memory_atom import CaptureResult
from ..storage.atom_store import AtomStore
from ..storage.diary_store import DiaryStore
from ..storage.persona_store import PersonaStore
from ..storage.conversation_store import ConversationStore
from ..storage.write_op_log import WriteOpLog
from ..storage.graph_store import GraphStore
from ..storage.index_validator import IndexValidator
from ..storage.db_migration import DBMigration
from ..storage.base_store import BaseDbStore
from .adapters import LLMProvider, ContextProvider, EmbeddingProvider

# 接口（供类型标注用）
from .interfaces import (
    ICapturer, IRetriever, IPersonaEngine, IGraphEngine,
    IWarmProcessor, IConsolidationManager, ICommandHandler,
    IMemoryInjector,
)

# 具体实现（供工厂实例化用）
from ..pipeline.capturer import Capturer
from ..pipeline.memory_uow import MemoryUnitOfWork
from ..features.persona_engine import PersonaEngine
from .retriever import Retriever
from ..pipeline.warm_processor import WarmProcessor
from .memory_injector import MemoryInjector
from ..pipeline.consolidation_manager import ConsolidationManager
from ..features.command_handler import CommandHandler
from ..features.graph_engine import GraphEngine


@dataclass
class MemoryCoreOptions:
    """MemoryCore 可选配置 — ISP: 调用方只需传入关心的字段

    llm_provider 和 context_provider 是核心依赖，保持为显式参数。
    其余可选项（存储层覆盖、回复回调、配置字典等）聚合在此。
    """

    config: dict[str, Any] | None = None
    data_dir: str | None = None
    reply_handler: Callable[[str, str], None] | None = None
    # 存储层覆盖（测试用，默认从 data_dir 自动创建）
    atom_store: AtomStore | None = None
    diary_store: DiaryStore | None = None
    persona_store: PersonaStore | None = None
    graph_store: GraphStore | None = None
    conversation_store: ConversationStore | None = None
    write_op_log: WriteOpLog | None = None
    # 嵌入模型（可选，默认不启用向量检索）
    embed_provider: EmbeddingProvider | None = None


class MemoryCore:
    """
    记忆核心 — 门面

    统一初始化、生命周期管理、模块装配。
    不依赖任何框架，LLMProvider / ContextProvider 由外部注入。
    """

    def __init__(
        self,
        llm_provider: LLMProvider | None = None,
        context_provider: ContextProvider | None = None,
        options: MemoryCoreOptions | None = None,
        # 以下为旧版平铺参数，已弃用但仍支持（通过 options 兼容）
        config: dict[str, Any] | None = None,
        data_dir: str | None = None,
        reply_handler: Callable[[str, str], None] | None = None,
        atom_store: AtomStore | None = None,
        diary_store: DiaryStore | None = None,
        persona_store: PersonaStore | None = None,
        graph_store: GraphStore | None = None,
        conversation_store: ConversationStore | None = None,
        write_op_log: WriteOpLog | None = None,
    ):
        # 新旧兼容：options 优先，旧参数兜底
        opts = options or MemoryCoreOptions()
        self.config = opts.config if opts.config is not None else (config or {})
        self._initialized = False

        # 外部注入的依赖
        self._injected = {
            "llm_provider": llm_provider,
            "context_provider": context_provider,
            "atom_store": opts.atom_store or atom_store,
            "diary_store": opts.diary_store or diary_store,
            "persona_store": opts.persona_store or persona_store,
            "graph_store": opts.graph_store or graph_store,
            "conversation_store": opts.conversation_store or conversation_store,
            "write_op_log": opts.write_op_log or write_op_log,
        }

        # 回复回调
        self.reply_handler = opts.reply_handler or reply_handler

        self.data_dir = Path(opts.data_dir or data_dir or ".")

        # 子模块（标注为接口类型，实现由工厂类注入）
        self.llm_provider: LLMProvider | None = None
        self.context_provider: ContextProvider | None = None
        self.embed_provider: EmbeddingProvider | None = opts.embed_provider
        self.atom_store: AtomStore | None = None
        self.diary_store: DiaryStore | None = None
        self.persona_store: PersonaStore | None = None
        self.capturer: ICapturer | None = None
        self.persona_engine: IPersonaEngine | None = None
        self.retriever: IRetriever | None = None
        self.injector: IMemoryInjector | None = None
        self.consolidation_manager: IConsolidationManager | None = None
        self.warm_processor: IWarmProcessor | None = None
        self.command_handler: ICommandHandler | None = None
        self.graph_store: GraphStore | None = None
        self.graph_engine: IGraphEngine | None = None
        self.conversation_store: ConversationStore | None = None
        self.write_op_log: WriteOpLog | None = None
        self.page_api = None
        self._background_tasks: set[asyncio.Task] = set()

    async def initialize(self):
        """初始化所有模块 — 按阶段拆解"""
        if self._initialized:
            return

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._prompts_dir = str(Path(__file__).parent.parent / "prompts")
        self._db_path = str(self.data_dir / "memory.db")
        self._diaries_db = str(self.data_dir / "diaries.db")
        self._conversations_db = str(self.data_dir / "conversations.db")
        self._graph_db = str(self.data_dir / "graph.db")

        await self._phase1_core_deps()
        await self._phase2_db_migration()
        await self._phase3_stores()
        await self._phase4_data_recovery()
        self._phase5_graph_engine()
        await self._phase6_business_modules()
        await self._phase7_background_processing()
        await self._phase8_scheduler()

        self._initialized = True

    # ── 初始化阶段 ────────────────────────────────────────

    async def _phase1_core_deps(self):
        """Phase 1: 核心依赖注入"""
        inj = self._injected
        self.llm_provider = inj["llm_provider"]
        self.context_provider = inj["context_provider"]
        if not self.llm_provider or not self.context_provider:
            raise RuntimeError("必须提供 llm_provider 和 context_provider")

    async def _phase2_db_migration(self):
        """Phase 2: 数据库迁移（每个 DB 独立版本）"""
        for path, scope in [
            (self._db_path, "memory"),
            (self._diaries_db, "diaries"),
            (self._conversations_db, "conversations"),
            (self._graph_db, "graph"),
        ]:
            try:
                migration = DBMigration(path, scope=scope)
                await migration.initialize()
                await migration.migrate()
            except Exception as e:
                logger.warning(f"[Memoria] 数据库迁移失败 ({scope}): {e}")
        logger.info("[Memoria] 数据库迁移完成")

    async def _phase3_stores(self):
        """Phase 3: 存储层初始化"""
        inj = self._injected
        self.atom_store = inj["atom_store"] or AtomStore(self._db_path)
        self.diary_store = inj["diary_store"] or DiaryStore(self._diaries_db)
        self.persona_store = inj["persona_store"] or PersonaStore(str(self.data_dir))
        self.graph_store = inj["graph_store"] or GraphStore(self._graph_db)
        self.conversation_store = inj["conversation_store"] or ConversationStore(self._conversations_db)
        self.write_op_log = inj["write_op_log"] or WriteOpLog(self._db_path)

        results = await asyncio.gather(
            self.atom_store.initialize(),
            self.diary_store.initialize(),
            self.graph_store.initialize(),
            self.conversation_store.initialize(),
            self.write_op_log.initialize(),
            return_exceptions=True,
        )
        names = ["atom_store", "diary_store", "graph_store", "conversation_store", "write_op_log"]
        for name, result in zip(names, results):
            if isinstance(result, Exception):
                logger.warning(f"[Memoria] {name} 初始化异常: {result}")

    async def _phase4_data_recovery(self):
        """Phase 4: 数据恢复 + 旧数据迁移"""
        # 写操作日志修复
        try:
            await self.write_op_log.repair_on_startup()
        except Exception as e:
            logger.warning(f"[Memoria] 写操作日志修复失败: {e}")

        # Bot 身份
        if self.atom_store:
            try:
                bot_name = self.config.get("bot_name", "Hana")
                await self.atom_store.init_bot_identity(bot_name)
            except Exception as e:
                logger.warning(f"[Memoria] 初始化 bot 身份失败: {e}")

        # 旧数据迁移（memory.db → 分库）
        try:
            await self._maybe_copy_legacy_data(self._db_path)
        except Exception as e:
            logger.warning(f"[Memoria] 旧数据迁移异常: {e}")

        # 索引检查（后台）
        task = asyncio.ensure_future(self._async_index_check(self._db_path))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _phase5_graph_engine(self):
        """Phase 5: 图谱引擎（通过 IUserIdentityResolver 解耦）"""
        from ..core.adapters import CoreUserIdentityResolver
        resolver = CoreUserIdentityResolver(self.atom_store)
        self.graph_engine = GraphEngine(
            graph_store=self.graph_store,
            diary_store=self.diary_store,
            config=self.config,
            embed_provider=self.embed_provider,
            user_identity_resolver=resolver,
        )

    async def _phase6_business_modules(self):
        """Phase 6: 业务逻辑模块装配"""
        # 存储门面
        self._memory_uow = MemoryUnitOfWork(
            diary_store=self.diary_store,
            atom_store=self.atom_store,
            write_op_log=self.write_op_log,
        )

        # 嵌入模型：优先用外部传入，否则根据配置自动创建
        if self.embed_provider is None and self.config.get("embed_provider_id"):
            self._init_embed_provider(self.config["embed_provider_id"])

        # Capturer
        self.capturer = Capturer(
            llm_provider=self.llm_provider,
            store=self._memory_uow,
            prompts_dir=self._prompts_dir,
            config=self.config,
            on_atoms_created=self.graph_engine.index_diary,
            embed_provider=self.embed_provider,
        )

        # 生命周期管理器
        self.lifecycle = None
        try:
            from ..lifecycle import LifecycleManager
            self.lifecycle = LifecycleManager(
                atom_store=self.atom_store,
                diary_store=self.diary_store,
                embed_provider=self.embed_provider,
                config={
                    "decay_rate": self.config.get("decay_rate", 0.99),
                    "decay_enabled": self.config.get("decay_enabled", True),
                    "expired_atom_ttl_days": self.config.get("expired_atom_ttl_days", 60),
                    "archive": self.config.get("archive", {}),
                    "archive_path": self.config.get("archive", {}).get("path", "./memory_archive"),
                    "orphan_importance_threshold": self.config.get("orphan_importance_threshold", 0.2),
                },
            )
            self.capturer.lifecycle = self.lifecycle

            # 启动时清理孤立原子
            try:
                cleaned = await self.lifecycle.cleanup.cleanup_orphans()
                if cleaned > 0:
                    logger.info(f"[Memoria] 启动时清理了 {cleaned} 条孤立原子")
            except Exception as e:
                logger.warning(f"[Memoria] 启动孤立原子清理失败: {e}")
        except Exception as e:
            logger.warning(f"[Memoria] 生命周期管理器初始化失败: {e}")

        # Persona / Retriever / Injector
        self.persona_engine = PersonaEngine(
            llm_provider=self.llm_provider,
            atom_store=self.atom_store,
            diary_store=self.diary_store,
            capturer=self.capturer,
            prompts_dir=self._prompts_dir,
            config=self.config,
            embed_provider=self.embed_provider,
        )
        self.retriever = Retriever(
            atom_store=self.atom_store,
            persona_store=self.persona_store,
            diary_store=self.diary_store,
            config=self.config,
            conversation_store=self.conversation_store,
            graph_store=self.graph_store,
            embed_provider=self.embed_provider,
        )
        self.injector = MemoryInjector(self.config)

    async def _phase7_background_processing(self):
        """Phase 7: 后台处理队列 + 定时循环"""
        # 暖处理队列
        self.warm_processor = WarmProcessor(
            capturer=self.capturer,
            graph_engine=self.graph_engine,
            persona_engine=self.persona_engine,
            config=self.config,
        )
        await self.warm_processor.start()

        # 定时循环（各自独立 try，一个失败不影响其他）
        loops = [
            ("cleanup", self._cleanup_loop()),
            ("co_occur", self._cooccur_loop()),
        ]
        if self.lifecycle:
            loops.append(("lifecycle", self._lifecycle_loops()))

        for name, coro in loops:
            try:
                task = asyncio.ensure_future(coro)
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
            except Exception as e:
                logger.warning(f"[Memoria] {name} 定时循环注册失败: {e}")

    async def _phase8_scheduler(self):
        """Phase 8: 调度器 + 指令处理器"""
        self.consolidation_manager = ConsolidationManager(
            conversation_store=self.conversation_store,
            warm_processor=self.warm_processor,
            config=self.config,
        )
        await self.consolidation_manager.initialize()

        self.command_handler = CommandHandler(
            diary_store=self.diary_store,
            atom_store=self.atom_store,
            persona_store=self.persona_store,
            retriever=self.retriever,
            capturer=self.capturer,
        )

    async def set_page_api(self, page_api) -> None:
        """外部注入 WebUI API（不强制，不用也可以）"""
        self.page_api = page_api

    # ═══════════════════════════════════════════════════
    #  后台任务
    # ═══════════════════════════════════════════════════

    async def _async_index_check(self, db_path: str):
        try:
            validator = IndexValidator(db_path)
            results = await validator.validate_all()
            if not results["summary"]["all_passed"]:
                for name, r in results.items():
                    if name != "summary" and not r.get("passed", False):
                        for issue in r.get("issues", []):
                            logger.warning(f"[Memoria] 索引检查: {issue}")

            # 孤立原子检查
            if self.atom_store and self.diary_store:
                try:
                    orphan_rows = await self.atom_store.fetch(
                        "SELECT DISTINCT diary_date FROM memory_atoms "
                        "WHERE status='active' AND diary_date != ''"
                    )
                    orphan_count = 0
                    for (date_str,) in orphan_rows:
                        row = await self.diary_store.fetchone(
                            "SELECT 1 FROM diary_entries WHERE date=? LIMIT 1",
                            (date_str,),
                        )
                        if not row:
                            orphan_count += 1
                    if orphan_count > 0:
                        logger.warning(
                            f"[Memoria] 孤立原子检查: {orphan_count} 条日记日期无对应日记"
                        )
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"[Memoria] 索引检查失败: {e}")

    async def _lifecycle_loops(self):
        while not self._initialized:
            await asyncio.sleep(3600)
        while True:
            try:
                await asyncio.sleep(86400)
                if not self.lifecycle:
                    continue
                await self.lifecycle.run_daily_maintenance()
                await self.lifecycle.run_daily_archive()
                await self.lifecycle.run_daily_cleanup()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[Memoria] 生命周期循环异常: {e}")
                await asyncio.sleep(3600)

    async def _maybe_copy_legacy_data(self, old_db: str):
        """一次性的旧数据迁移：从旧单库 memory.db 分拆到各独立数据库

        启动时自动检测 + 执行，完成后不再重复。
        数据留在旧库不动（安全保留），后续完全由新库提供服务。
        """
        if not Path(old_db).exists():
            return

        import aiosqlite

        # 检查旧库是否有需要迁移的表
        async with aiosqlite.connect(old_db) as src:
            for table, dest_store, dest_path, dest_cols in [
                ("diary_entries", self.diary_store,
                 str(self.data_dir / "diaries.db"),
                 ["id", "user_id", "date", "content", "topics", "sentiment",
                  "importance", "atom_count", "created_at", "updated_at", "status", "archived"]),
                ("sessions", self.conversation_store,
                 str(self.data_dir / "conversations.db"),
                 None),
                ("messages", self.conversation_store,
                 str(self.data_dir / "conversations.db"),
                 None),
            ]:
                if not dest_store:
                    continue

                try:
                    has = await src.execute_fetchall(
                        "SELECT COUNT(*) FROM sqlite_master "
                        "WHERE type='table' AND name=?",
                        (table,),
                    )
                    if not has or not has[0][0]:
                        continue

                    src_count = (await src.execute_fetchall(
                        f"SELECT COUNT(*) FROM {table}",
                    ))[0][0]
                    if src_count == 0:
                        continue

                    dest_count = (await dest_store.fetchone(
                        f"SELECT COUNT(*) FROM {table}",
                    ))[0]
                    if dest_count > 0:
                        continue  # 目标库已有数据，跳过
                except Exception:
                    continue

                # 复制数据
                try:
                    src_rows = await src.execute_fetchall(f"SELECT * FROM {table}")

                    if dest_cols:
                        cols = ",".join(dest_cols)
                        placeholders = ",".join("?" for _ in dest_cols)
                        sql = f"INSERT OR IGNORE INTO {table}({cols}) VALUES ({placeholders})"
                    else:
                        # 自动探测列数
                        info = await src.execute_fetchall(f"PRAGMA table_info({table})")
                        col_names = [r[1] for r in info]
                        cols = ",".join(col_names)
                        placeholders = ",".join("?" for _ in col_names)
                        sql = f"INSERT OR IGNORE INTO {table}({cols}) VALUES ({placeholders})"

                    async with aiosqlite.connect(dest_path) as dst:
                        for p in ["PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL"]:
                            try:
                                await dst.execute(p)
                            except Exception:
                                pass
                        for row in src_rows:
                            try:
                                await dst.execute(sql, row)
                            except Exception:
                                pass
                        await dst.commit()

                    logger.info(f"[Memoria] 旧数据迁移: {src_count} 行 → {Path(dest_path).name}/{table}")

                    # 特殊处理：重建 diary FTS
                    if table == "diary_entries" and dest_store == self.diary_store:
                        try:
                            async with aiosqlite.connect(dest_path) as dst:
                                await dst.execute(
                                    "INSERT INTO diary_fts(diary_fts) VALUES('rebuild')"
                                )
                                await dst.commit()
                        except Exception:
                            pass

                except Exception as e:
                    logger.warning(f"[Memoria] 旧数据迁移失败 {table}: {e}")

        logger.info("[Memoria] 旧数据迁移完成")

    async def _cleanup_loop(self):
        """定时清理：对话滑动窗口"""
        while not self._initialized:
            await asyncio.sleep(5)
        while True:
            try:
                await asyncio.sleep(120)
                if self.conversation_store:
                    deleted = await self.conversation_store.enforce_retention()
                    if deleted:
                        logger.info(f"[Memoria] 对话滑动窗口清理: {deleted} 条")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[Memoria] 清理异常: {e}")

    async def _cooccur_loop(self):
        while not self._initialized:
            await asyncio.sleep(3600)
        while True:
            try:
                await asyncio.sleep(86400)
                if not self.graph_engine:
                    continue
                count = await self.graph_engine.batch_cooccur()
                logger.info(f"[Memoria] co_occur 统计更新: {count} 条")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[Memoria] co_occur 更新异常: {e}")
                await asyncio.sleep(3600)

    # ═══════════════════════════════════════════════════
    #  对外 API
    # ═══════════════════════════════════════════════════

    async def process_message(
        self,
        user_id: str,
        message_text: str,
        sender_name: str = "",
        system_prompt: str = "",
        event_extra: dict | None = None,
    ) -> str | None:
        """处理一条用户消息

        返回修改后的用户消息文本（含注入的记忆），
        或 None（记忆已注入到 system_prompt，消息无变化）。
        """
        if not self._initialized:
            return None
        if not user_id or not message_text:
            return None

        # 指令
        if message_text.startswith("/"):
            await self._handle_command(user_id, message_text)
            return None

        # 召回记忆并注入
        recall_result = await self.retriever.get_context_memories(user_id, message_text)

        if not recall_result.memory_text and not recall_result.persona_text:
            return None

        new_system, new_user = self.injector.inject(
            memory_text=recall_result.memory_text,
            persona_text=recall_result.persona_text,
            system_prompt=system_prompt,
            user_message=message_text,
            user_name=sender_name or user_id,
        )

        if new_system != system_prompt:
            return new_user  # 调用方需要更新 system_prompt

        if new_user != message_text:
            return new_user

        return None

    async def trigger_capture(self, user_id: str, text: str):
        """后台触发记忆整理"""
        try:
            task = asyncio.ensure_future(
                self.consolidation_manager.on_message(user_id, text)
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        except Exception:
            pass

    async def add_agent_memory(
        self,
        user_id: str,
        memory: str,
        key_facts: list[str] | None = None,
        topics: list[str] | None = None,
        sentiment: str = "neutral",
        importance: float = 0.5,
    ) -> dict:
        """Agent 主动写入记忆

        供外部 Agent 框架的工具（Tool Call）调用。
        不做 LLM 调用，纯格式化 + 规则分类 + 落库。

        Args:
            user_id: 用户标识
            memory: 记忆摘要文本（对应日记内容）
            key_facts: 关键事实列表（将被规则分类为原子）
            topics: 主题标签
            sentiment: 情感 positive / neutral / negative
            importance: 重要性 0~1

        Returns:
            {"id": int, "atom_count": int, "status": "ok"}
        """
        if not self._initialized or not user_id or not memory:
            return {"id": 0, "atom_count": 0, "status": "skipped"}

        today = time.strftime("%Y-%m-%d")

        # 1. 写日记
        from ..utils.diary_helper import build_diary_content
        fm = {
            "date": today,
            "mood": sentiment,
            "importance": importance,
            "topics": topics or [],
        }
        diary_content = build_diary_content(fm, memory)
        diary_id = await self.diary_store.append(today, diary_content)

        # 2. 原子分类 + 落库
        atoms: list[MemoryAtom] = []
        if key_facts and self.atom_store:
            from ..pipeline.atom_classifier import classify_atoms
            atoms = classify_atoms(
                key_facts=key_facts,
                entities=topics or [],
                parent_importance=importance,
                user_id=user_id,
                diary_date=today,
            )
            for atom in atoms:
                atom.diary_id = diary_id
                atom.prepare_insert()
            if atoms:
                ids = await self.atom_store.insert_many(atoms)
                for atom, aid in zip(atoms, ids):
                    atom.atom_id = aid
                    # 桥表关联（多对多）
                    try:
                        await self.atom_store.link_atom_to_diary(
                            aid, diary_id,
                            snippet=atom.diary_snippet,
                            importance=atom.importance,
                        )
                    except Exception:
                        pass

        return {
            "id": diary_id,
            "atom_count": len(atoms),
            "status": "ok",
        }

    async def search_agent_memory(
        self,
        user_id: str,
        query: str,
        k: int = 5,
    ) -> list[dict]:
        """Agent 主动搜索记忆

        供外部 Agent 框架的工具（Tool Call）调用。
        返回结构化的记忆列表，不含 LLM 调用。

        Args:
            user_id: 用户标识
            query: 搜索关键词
            k: 返回条数

        Returns:
            [{"id": int, "content": str, "type": str, "importance": float,
              "date": str, "entities": list[str], "score": float}, ...]
        """
        if not self._initialized or not self.retriever:
            return []

        atoms = await self.retriever.recall(user_id, query, k)
        results = []
        for atom in atoms:
            results.append({
                "id": atom.atom_id,
                "content": atom.content,
                "type": atom.atom_type.value,
                "importance": atom.importance,
                "date": atom.diary_date,
                "entities": atom.entities,
                "confidence": atom.confidence,
            })
        return results

    async def _handle_command(self, user_id: str, message: str):
        parts = message.strip().split()
        if not parts:
            return
        cmd = parts[0].lower()
        args = parts[1:]

        handler_map = {
            "/日记": self.command_handler.handle_diary,
            "/日记列表": self.command_handler.handle_diary_list,
            "/记忆": self.command_handler.handle_memory,
            "/记忆搜索": lambda uid, a: self.command_handler.handle_search(uid, " ".join(a)),
            "/记忆删除": self.command_handler.handle_delete,
            "/记忆统计": lambda uid, a: self.command_handler.handle_stats(uid),
            "/记忆重构": lambda uid, a: self.command_handler.handle_rebuild(uid, a),
        }

        handler = handler_map.get(cmd)
        if not handler:
            if cmd == "/日记" and args:
                handler = self.command_handler.handle_diary
            else:
                return

        try:
            result = await handler(user_id, args)
            if self.reply_handler:
                self.reply_handler(user_id, result)
        except Exception as e:
            logger.warning(f"[Memoria] 指令处理失败 {cmd}: {e}")

    async def destroy(self):
        """优雅关闭 — 停后台任务"""
        # 1) 停后台任务
        if self.warm_processor:
            await self.warm_processor.stop()
        if self.consolidation_manager:
            await self.consolidation_manager.destroy()
        for task in list(self._background_tasks):
            if not task.done():
                task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()
        try:
            await BaseDbStore.close_all()
        except Exception as e:
            logger.warning(f"[Memoria] 关闭连接池异常: {e}")
            try:
                BaseDbStore.close_all_sync()
            except Exception:
                pass
        self._initialized = False

    def _init_embed_provider(self, embed_id: str):
        """根据 embed_provider_id 动态创建/切换 EmbeddingProvider

        支持三种后端（对应模型提供商中的 type）:
          - "local"           → LocalEmbeddingProvider (sentence-transformers)
          - type="embed:local"  → LocalEmbeddingProvider
          - type="embed:ollama" → OllamaEmbeddingProvider
          - type="embed:api"    → RemoteEmbeddingProvider
        """
        if not embed_id:
            self.embed_provider = None
            return

        from ..core.embed_providers import create_embed_provider

        model_name = self.config.get("embed_model_name", "BAAI/bge-m3")
        providers = self.config.get("_providers", [])
        new_provider = create_embed_provider(embed_id, providers, model_name)

        if new_provider is not None:
            self.embed_provider = new_provider
            logger.info(
                "[MemoryCore] 嵌入模型已切换: %s (%s)",
                embed_id,
                type(new_provider).__name__,
            )
            # 同步给下游模块
            if hasattr(self, "capturer") and self.capturer:
                self.capturer.embed_provider = new_provider
            if hasattr(self, "lifecycle") and self.lifecycle:
                self.lifecycle.embed_provider = new_provider
        else:
            logger.warning("[MemoryCore] 嵌入提供商 %r 未找到，禁用向量检索", embed_id)
            self.embed_provider = None

    def reload_config(self, config: dict[str, Any]):
        self.config.update(config)
        if self.injector:
            self.injector.reload_config(self.config)
        if self.consolidation_manager:
            self.consolidation_manager.update_config(config)
        # 切换 LLM 模型
        if self.llm_provider:
            if "llm_provider_id" in config:
                self.llm_provider.set_provider(config["llm_provider_id"])
            if "judge_provider_id" in config:
                self.llm_provider.set_judge_provider(config["judge_provider_id"])
        # 切换嵌入模型
        if "embed_provider_id" in config:
            self._init_embed_provider(config["embed_provider_id"])

    # 向后兼容 — 旧的 memory_core.on_message 接口
    async def on_message(self, event, sender_name: str = "") -> str | None:
        """兼容接口：通过 context_provider 从 event 提取信息后调用 process_message"""
        if not self._initialized:
            return None
        user_id = self.context_provider.get_user_id(event)
        message_text = self.context_provider.get_conversation_text(event)
        return await self.process_message(
            user_id=user_id,
            message_text=message_text,
            sender_name=sender_name,
            system_prompt=getattr(event, "system_prompt", "") or "",
            event_extra={"event": event},
        )
