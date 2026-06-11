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
from ..storage.state_store import StateStore
from ..storage.conversation_store import ConversationStore
from ..storage.write_op_log import WriteOpLog
from ..storage.graph_store import GraphStore
from ..storage.index_validator import IndexValidator
from ..storage.db_migration import DBMigration
from ..storage.base_store import BaseDbStore
from .adapters import LLMProvider, ContextProvider

# 接口（供类型标注用）
from .interfaces import (
    ICapturer, IRetriever, IPersonaEngine, IGraphEngine,
    IWarmProcessor, IConsolidationManager, ICommandHandler,
    IMemoryInjector, IHotMessageCache,
)

# 具体实现（供工厂实例化用）
from ..pipeline.capturer import Capturer
from ..pipeline.memory_uow import MemoryUnitOfWork
from ..features.persona_engine import PersonaEngine
from .retriever import Retriever
from .hot_cache import HotMessageCache
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
    state_store: StateStore | None = None
    graph_store: GraphStore | None = None
    conversation_store: ConversationStore | None = None
    write_op_log: WriteOpLog | None = None


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
        state_store: StateStore | None = None,
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
            "state_store": opts.state_store or state_store,
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
        self.atom_store: AtomStore | None = None
        self.diary_store: DiaryStore | None = None
        self.persona_store: PersonaStore | None = None
        self.state_store: StateStore | None = None
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
        self.hot_cache: IHotMessageCache = HotMessageCache()
        self._background_tasks: set[asyncio.Task] = set()

    async def initialize(self):
        """初始化所有模块"""
        if self._initialized:
            return

        self.data_dir.mkdir(parents=True, exist_ok=True)

        prompts_dir = str(Path(__file__).parent.parent / "prompts")
        db_path = str(self.data_dir / "memory.db")
        inj = self._injected

        # 1. 抽象层（必须注入，无默认实现）
        self.llm_provider = inj["llm_provider"]
        self.context_provider = inj["context_provider"]
        if not self.llm_provider or not self.context_provider:
            raise RuntimeError("必须提供 llm_provider 和 context_provider")

        # 2. 数据库迁移
        try:
            migration = DBMigration(db_path)
            await migration.initialize()
            await migration.migrate()
            logger.info("[Memoria] 数据库迁移完成")
        except Exception as e:
            logger.warning(f"[Memoria] 数据库迁移失败（不影响启动）: {e}")

        # 3. 存储层
        self.atom_store = inj["atom_store"] or AtomStore(db_path)
        self.diary_store = inj["diary_store"] or DiaryStore(db_path)
        self.persona_store = inj["persona_store"] or PersonaStore(str(self.data_dir))
        self.state_store = inj["state_store"] or StateStore(db_path)
        self.graph_store = inj["graph_store"] or GraphStore(db_path)
        self.conversation_store = inj["conversation_store"] or ConversationStore(db_path)
        self.write_op_log = inj["write_op_log"] or WriteOpLog(db_path)

        # 并行初始化存储层
        init_tasks = [
            self.atom_store.initialize(),
            self.diary_store.initialize(),
            self.state_store.initialize(),
            self.graph_store.initialize(),
            self.conversation_store.initialize(),
            self.write_op_log.initialize(),
        ]
        await asyncio.gather(*init_tasks, return_exceptions=True)
        for i, task in enumerate(init_tasks):
            if isinstance(task, Exception):
                store_name = ["atom_store", "diary_store", "state_store", "graph_store", "conversation_store", "write_op_log"][i]
                logger.warning(f"[Memoria] {store_name} 初始化异常: {task}")

        # 启动时修复未完成的写操作
        try:
            await self.write_op_log.repair_on_startup()
        except Exception as e:
            logger.warning(f"[Memoria] 写操作日志修复失败: {e}")

        # 初始化 bot 身份
        if self.atom_store:
            try:
                bot_name = self.config.get("bot_name", "Hana")
                await self.atom_store.init_bot_identity(bot_name)
            except Exception as e:
                logger.warning(f"[Memoria] 初始化 bot 身份失败: {e}")

        # 兼容旧数据库列
        if self.diary_store:
            try:
                await self.diary_store.execute(
                    "ALTER TABLE diary_entries ADD COLUMN archived INTEGER DEFAULT 0"
                )
            except Exception:
                pass

        # 归档模块
        if self.diary_store and self.config.get("archive", {}).get("enabled", True):
            try:
                from .archiver import Archiver
                self.archiver = Archiver(
                    diary_store=self.diary_store,
                    archive_dir=self.config.get("archive", {}).get("path", "./memory_archive"),
                    config=self.config,
                )
                archive_task = asyncio.ensure_future(self._archive_loop())
                self._background_tasks.add(archive_task)
                archive_task.add_done_callback(self._background_tasks.discard)
            except Exception as e:
                logger.warning(f"[Memoria] 初始化归档模块失败: {e}")
        else:
            self.archiver = None

        # 索引一致性检查
        task = asyncio.ensure_future(self._async_index_check(db_path))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

        # 重要度衰减
        decay_task = asyncio.ensure_future(self._decay_loop())
        self._background_tasks.add(decay_task)
        decay_task.add_done_callback(self._background_tasks.discard)

        # 启动时清理孤立原子
        try:
            cleaned = await self._cleanup_orphan_atoms()
            if cleaned > 0:
                logger.info(f"[Memoria] 启动时清理了 {cleaned} 条孤立原子")
        except Exception:
            pass

        # 4. 图谱引擎
        self.graph_engine = GraphEngine(
            graph_store=self.graph_store,
            atom_store=self.atom_store,
            diary_store=self.diary_store,
        )

        # 后台图谱任务
        try:
            relate_task = asyncio.ensure_future(self._relates_to_loop())
            self._background_tasks.add(relate_task)
            relate_task.add_done_callback(self._background_tasks.discard)
            co_task = asyncio.ensure_future(self._cooccur_loop())
            self._background_tasks.add(co_task)
            co_task.add_done_callback(self._background_tasks.discard)
        except Exception as e:
            logger.warning(f"[Memoria] 图谱后台任务注册失败: {e}")

        # 5a. 存储门面（将 3 个 store 合并为一个 MemoryUnitOfWork）
        self._memory_uow = MemoryUnitOfWork(
            diary_store=self.diary_store,
            atom_store=self.atom_store,
            write_op_log=self.write_op_log,
        )

        # 5b. 核心业务模块
        self.capturer = Capturer(
            llm_provider=self.llm_provider,
            store=self._memory_uow,
            prompts_dir=prompts_dir,
            config=self.config,
            on_atoms_created=self.graph_engine.index_diary,
        )
        self.persona_engine = PersonaEngine(
            llm_provider=self.llm_provider,
            atom_store=self.atom_store,
            diary_store=self.diary_store,
            capturer=self.capturer,
            prompts_dir=prompts_dir,
            config=self.config,
        )
        self.retriever = Retriever(
            atom_store=self.atom_store,
            persona_store=self.persona_store,
            diary_store=self.diary_store,
            config=self.config,
            hot_cache=self.hot_cache,
            conversation_store=self.conversation_store,
            graph_store=self.graph_store,
        )
        self.injector = MemoryInjector(self.config)

        # 6. 后台暖处理队列
        self.warm_processor = WarmProcessor(
            capturer=self.capturer,
            graph_engine=self.graph_engine,
            persona_engine=self.persona_engine,
            config=self.config,
        )
        await self.warm_processor.start()

        # 7. 调度器
        self.consolidation_manager = ConsolidationManager(
            state_store=self.state_store,
            warm_processor=self.warm_processor,
            config=self.config,
        )
        await self.consolidation_manager.initialize()

        # 8. 指令处理器
        self.command_handler = CommandHandler(
            diary_store=self.diary_store,
            atom_store=self.atom_store,
            persona_store=self.persona_store,
            retriever=self.retriever,
            capturer=self.capturer,
        )

        self._initialized = True

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
        except Exception as e:
            logger.warning(f"[Memoria] 索引检查失败: {e}")

    async def _decay_loop(self):
        while not self._initialized:
            await asyncio.sleep(3600)
        while True:
            try:
                await asyncio.sleep(86400)
                if not self.atom_store:
                    continue
                rate = float(self.config.get("decay_rate", 0.99))
                enabled = self.config.get("decay_enabled", True)
                if not enabled or rate <= 0 or rate >= 1.0:
                    continue
                await self.atom_store.apply_decay(rate)
                await self.atom_store.execute(
                    f"UPDATE atomic_facts SET importance = importance * {rate} WHERE importance > 0.1"
                )
                logger.info(f"[Memoria] 重要度衰减完成 (rate={rate})")
                try:
                    await self._cleanup_expired_atoms()
                except Exception as e:
                    logger.warning(f"[Memoria] 过期原子清理异常: {e}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[Memoria] 重要度衰减异常: {e}")
                await asyncio.sleep(3600)

    async def _cleanup_orphan_atoms(self) -> int:
        if not self.atom_store:
            return 0
        cursor = await self.atom_store.execute("""
            UPDATE memory_atoms SET status='dormant'
            WHERE status='active' AND importance < 0.2 AND (
                diary_id = 0 OR
                (diary_id > 0 AND NOT EXISTS (
                    SELECT 1 FROM diary_entries de WHERE de.id = diary_id
                ))
            )
        """)
        count = cursor.rowcount if cursor else 0
        if count > 0:
            await self.atom_store.execute(
                "DELETE FROM memory_atoms_fts WHERE atom_id NOT IN (SELECT id FROM memory_atoms WHERE status IN ('active','dormant'))"
            )
        return count

    async def _cleanup_expired_atoms(self):
        if not self.atom_store:
            return 0
        ttl_days = float(self.config.get("expired_atom_ttl_days", 60))
        cutoff = time.time() - ttl_days * 86400
        cursor = await self.atom_store.execute(
            "DELETE FROM memory_atoms WHERE status IN ('dormant','forgotten') AND created_at < ?",
            (cutoff,),
        )
        count = cursor.rowcount if cursor else 0
        if count > 0:
            await self.atom_store.execute(
                "DELETE FROM memory_atoms_fts WHERE atom_id NOT IN (SELECT id FROM memory_atoms)"
            )
            logger.info(f"[Memoria] 清理了 {count} 条过期原子 (>{ttl_days:.0f}天)")
        return count

    async def _archive_loop(self):
        while not self._initialized:
            await asyncio.sleep(3600)
        while True:
            try:
                await asyncio.sleep(86400)
                if not hasattr(self, 'archiver') or not self.archiver:
                    continue
                archived = await self.archiver.archive_daily()
                if archived:
                    logger.info(f"[Memoria] 归档完成: {archived} 条")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[Memoria] 归档异常: {e}")
                await asyncio.sleep(3600)

    async def _relates_to_loop(self):
        while not self._initialized:
            await asyncio.sleep(3600)
        while True:
            try:
                await asyncio.sleep(86400)
                if not self.graph_engine:
                    continue
                created = await self.graph_engine.upgrade_cooccur_to_relates(min_count=3)
                if created:
                    logger.info(f"[Memoria] relates_to 边升级: {created} 条")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[Memoria] relates_to 升级异常: {e}")
                await asyncio.sleep(3600)

    async def _cooccur_loop(self):
        while not self._initialized:
            await asyncio.sleep(3600)
        while True:
            try:
                await asyncio.sleep(86400)
                if not self.graph_engine:
                    continue
                count = await self.graph_engine.batch_cooccur()
                logger.info(f"[Memoria] co_occur 批量重建: {count} 对")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[Memoria] co_occur 重建异常: {e}")
                await asyncio.sleep(3600)

    # ═══════════════════════════════════════════════════
    #  用户分层 + 消息过滤
    # ═══════════════════════════════════════════════════

    KEYWORD_TRIGGER = {"记住", "别忘了", "喜欢", "教我", "帮我", "拜托",
                       "SC", "舰长", "关注", "谢谢", "投喂", "礼物", "订阅"}

    async def _get_tier(self, user_id: str) -> str:
        try:
            row = await self.atom_store.fetchone(
                "SELECT tier FROM user_persona WHERE uid=?", (user_id,)
            )
            if row and row[0]:
                return row[0]
        except Exception:
            pass
        return "new"

    async def _maybe_update_tier(self, user_id: str):
        try:
            row = await self.atom_store.fetchone(
                "SELECT diary_count_since_full FROM user_persona WHERE uid=?",
                (user_id,),
            )
            if row and row[0] is not None and row[0] < 10:
                return
            now = time.time()
            recent = await self.atom_store.fetchone(
                "SELECT COUNT(*) FROM diary_entries WHERE user_id=? AND created_at > ?",
                (user_id, now - 30 * 86400),
            )
            msg_count = recent[0] if recent else 0
            if msg_count >= 10:
                tier = "core"
            elif msg_count >= 5:
                tier = "active"
            elif msg_count >= 1:
                tier = "occasional"
            else:
                tier = "new"
            await self.atom_store.execute(
                "UPDATE user_persona SET tier=?, diary_count_since_full=0 WHERE uid=?",
                (tier, user_id),
            )
        except Exception:
            pass

    async def should_ignore(self, user_id: str, text: str) -> bool:
        tier = await self._get_tier(user_id)
        if tier in ("core", "active"):
            return False
        if len(text) < 3:
            return True
        if len(set(text)) / max(len(text), 1) < 0.4:
            return True
        if all(c in "😂😍😊😭😘🥰😁😅🤣😏🙏💕✨😌😔😤😴🤔👀🔥" for c in text.strip()):
            return True
        if any(kw in text for kw in self.KEYWORD_TRIGGER):
            return False
        return True

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

        # 写入热缓存
        self.hot_cache.push(
            user_id=user_id,
            role="user",
            content=message_text,
            sender_name=sender_name,
            sender_id=user_id,
        )

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
        diary_id = await self.diary_store.append(user_id, today, diary_content)

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
        """优雅关闭"""
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
