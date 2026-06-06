"""记忆核心 — 门面：统一管理所有模块"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from astrbot.api import logger

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
from ..core.graph_engine import GraphEngine
from .adapters import LLMProvider, AstrBotLLMProvider, AstrBotContextProvider
from .capturer import Capturer
from .persona_engine import PersonaEngine
from .retriever import Retriever
from .memory_injector import MemoryInjector
from .consolidation_manager import ConsolidationManager
from .command_handler import CommandHandler


class MemoryCore:
    """
    记忆核心 — 门面

    统一初始化、生命周期管理、模块装配。
    所有对外 API 都从这里走。
    """

    def __init__(self, plugin_context, data_dir: str, config: dict[str, Any] | None = None):
        self.plugin_context = plugin_context
        self.data_dir = Path(data_dir)
        self.config = config or {}
        self._initialized = False

        # 子模块（在 initialize 中创建）
        self.llm_provider: AstrBotLLMProvider | None = None
        self.context_provider: AstrBotContextProvider | None = None
        self.atom_store: AtomStore | None = None
        self.diary_store: DiaryStore | None = None
        self.persona_store: PersonaStore | None = None
        self.state_store: StateStore | None = None
        self.capturer: Capturer | None = None
        self.persona_engine: PersonaEngine | None = None
        self.retriever: Retriever | None = None
        self.injector: MemoryInjector | None = None
        self.consolidation_manager: ConsolidationManager | None = None
        self.command_handler: CommandHandler | None = None
        self.graph_store: GraphStore | None = None
        self.graph_engine: GraphEngine | None = None
        self.conversation_store: ConversationStore | None = None
        self.write_op_log: WriteOpLog | None = None
        self._background_tasks: set[asyncio.Task] = set()

    async def initialize(self):
        """初始化所有模块"""
        if self._initialized:
            return

        self.data_dir.mkdir(parents=True, exist_ok=True)

        prompts_dir = str(Path(__file__).parent.parent / "prompts")
        db_path = str(self.data_dir / "memory.db")

        # 1. 抽象层
        self.llm_provider = AstrBotLLMProvider(self.plugin_context)
        self.context_provider = AstrBotContextProvider()

        # 2. 数据库迁移（失败不阻塞启动）
        try:
            migration = DBMigration(db_path)
            await migration.initialize()
            await migration.migrate()
            logger.info("[Memory] 数据库迁移完成")
        except Exception as e:
            logger.warning(f"[Memory] 数据库迁移失败（不影响启动）: {e}")

        # 3. 存储层（统一 db_path，共享连接池）
        self.atom_store = AtomStore(db_path)
        self.diary_store = DiaryStore(db_path)
        self.persona_store = PersonaStore(str(self.data_dir))
        self.state_store = StateStore(db_path)
        self.graph_store = GraphStore(db_path)
        self.conversation_store = ConversationStore(db_path)
        self.write_op_log = WriteOpLog(db_path)

        # 并行初始化存储层（都是 I/O 密集，可并发）
        init_tasks = [
            self.atom_store.initialize(),
            self.diary_store.initialize(),
            self.state_store.initialize(),
            self.graph_store.initialize(),
            self.conversation_store.initialize(),
            self.write_op_log.initialize(),
        ]
        await asyncio.gather(*init_tasks, return_exceptions=True)
        # 记录失败的初始化
        for i, task in enumerate(init_tasks):
            if isinstance(task, Exception):
                store_name = ["atom_store", "diary_store", "state_store", "graph_store", "conversation_store", "write_op_log"][i]
                logger.warning(f"[Memory] {store_name} 初始化异常: {task}")

        # 启动时修复未完成的写操作
        try:
            await self.write_op_log.repair_on_startup()
        except Exception as e:
            logger.warning(f"[Memory] 写操作日志修复失败: {e}")

        # 索引一致性检查（异步，不阻塞初始化）
        task = asyncio.ensure_future(self._async_index_check(db_path))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

        # 4. 图谱引擎
        self.graph_engine = GraphEngine(
            graph_store=self.graph_store,
            atom_store=self.atom_store,
            diary_store=self.diary_store,
        )

        # 5. 核心业务模块
        self.capturer = Capturer(
            llm_provider=self.llm_provider,
            diary_store=self.diary_store,
            atom_store=self.atom_store,
            prompts_dir=prompts_dir,
            config=self.config,
            on_atoms_created=self.graph_engine.index_atom,
            write_op_log=self.write_op_log,
        )
        self.persona_engine = PersonaEngine(
            llm_provider=self.llm_provider,
            persona_store=self.persona_store,
            diary_store=self.diary_store,
            atom_store=self.atom_store,
            capturer=self.capturer,
            prompts_dir=prompts_dir,
            config=self.config,
        )
        self.retriever = Retriever(
            atom_store=self.atom_store,
            persona_store=self.persona_store,
            config=self.config,
        )
        self.injector = MemoryInjector(self.config)

        # 6. 调度器
        self.consolidation_manager = ConsolidationManager(
            capturer=self.capturer,
            persona_engine=self.persona_engine,
            state_store=self.state_store,
            config=self.config,
        )
        await self.consolidation_manager.initialize()

        # 7. WebUI API
        try:
            from .page_api import PageApi
            self.page_api = PageApi(self)
            self.page_api.register_routes(self.plugin_context)
        except Exception as e:
            logger.warning(f"[Memory] 注册 WebUI API 失败: {e}")

        # 8. 指令处理器
        self.command_handler = CommandHandler(
            diary_store=self.diary_store,
            atom_store=self.atom_store,
            persona_store=self.persona_store,
            retriever=self.retriever,
        )

        self._apply_provider_config()
        self._initialized = True

    async def _async_index_check(self, db_path: str):
        """后台索引一致性检查（不阻塞启动）"""
        try:
            validator = IndexValidator(db_path)
            results = await validator.validate_all()
            if not results["summary"]["all_passed"]:
                for name, r in results.items():
                    if name != "summary" and not r.get("passed", False):
                        for issue in r.get("issues", []):
                            logger.warning(f"[Memory] 索引检查: {issue}")
        except Exception as e:
            logger.warning(f"[Memory] 索引检查失败: {e}")

    async def destroy(self):
        """优雅关闭所有模块"""
        if self.consolidation_manager:
            await self.consolidation_manager.destroy()

        # 取消所有后台任务
        for task in list(self._background_tasks):
            if not task.done():
                task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()

        # 关闭数据库连接池
        try:
            await BaseDbStore.close_all()
        except Exception as e:
            logger.warning(f"[Memory] 关闭连接池异常: {e}")

        self._initialized = False

    async def on_message(self, event) -> str | None:
        """
        消息处理入口

        三步走：
        1. 检索相关记忆并注入
        2. 判断是否需要整理
        3. 处理指令
        """
        if not self._initialized:
            return None

        user_id = self.context_provider.get_user_id(event)
        message_text = self.context_provider.get_conversation_text(event)

        if not user_id or not message_text:
            return None

        # 检查是否是指令
        if message_text.startswith("/"):
            await self._handle_command(user_id, message_text)
            return None

        # 1. 召回记忆并注入
        recall_result = await self.retriever.get_context_memories(user_id, message_text)

        if recall_result.memory_text or recall_result.persona_text:
            system_prompt = getattr(event, "system_prompt", "") or ""
            user_message = message_text
            user_name = user_id

            new_system, new_user = self.injector.inject(
                memory_text=recall_result.memory_text,
                persona_text=recall_result.persona_text,
                system_prompt=system_prompt,
                user_message=user_message,
                user_name=user_name,
            )

            if new_system != system_prompt:
                event.system_prompt = new_system

            if new_user != user_message:
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

    async def _handle_command(self, user_id: str, message: str):
        """处理指令"""
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
        }

        handler = handler_map.get(cmd)
        if not handler:
            if cmd == "/日记" and args:
                handler = self.command_handler.handle_diary
            else:
                return

        try:
            result = await handler(user_id, args)
            if hasattr(self.plugin_context, "reply"):
                await self.plugin_context.reply(result)
        except Exception as e:
            logger.warning(f"[Memory] 指令处理失败 {cmd}: {e}")

    def reload_config(self, config: dict[str, Any]):
        """热加载配置"""
        self.config.update(config)
        if self.injector:
            self.injector.reload_config(self.config)
        if self.consolidation_manager:
            cm = self.consolidation_manager
            cm.trigger_msg_count = config.get("trigger_msg_count", cm.trigger_msg_count)
            cm.trigger_time_minutes = config.get("trigger_time_minutes", cm.trigger_time_minutes)
            cm.immediate_capture = config.get("immediate_capture", cm.immediate_capture)
            cm.warmup_enabled = config.get("warmup_enabled", cm.warmup_enabled)
            cm.persona_update_interval = config.get("persona_update_interval", cm.persona_update_interval)
        self._apply_provider_config()

    def _apply_provider_config(self):
        """应用 provider 配置"""
        provider_id = self.config.get("llm_provider") or None
        if self.llm_provider:
            self.llm_provider.set_provider(provider_id)
