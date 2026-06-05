"""记忆核心 — 门面：统一管理所有模块"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..models.memory_atom import CaptureResult
from ..storage.atom_store import AtomStore
from ..storage.diary_store import DiaryStore
from ..storage.persona_store import PersonaStore
from ..storage.state_store import StateStore
from ..storage.graph_store import GraphStore
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

    async def initialize(self):
        """初始化所有模块"""
        if self._initialized:
            return

        # 创建数据目录
        self.data_dir.mkdir(parents=True, exist_ok=True)

        prompts_dir = str(Path(__file__).parent.parent / "prompts")
        db_path = str(self.data_dir / "memory.db")

        # 1. 抽象层
        self.llm_provider = AstrBotLLMProvider(self.plugin_context)
        self.context_provider = AstrBotContextProvider()

        # 2. 存储层
        self.atom_store = AtomStore(db_path)
        self.diary_store = DiaryStore(db_path)
        self.persona_store = PersonaStore(str(self.data_dir))
        self.state_store = StateStore(db_path)
        self.graph_store = GraphStore(db_path)

        await self.atom_store.initialize()
        await self.diary_store.initialize()
        await self.state_store.initialize()
        await self.graph_store.initialize()

        # 3. 图谱引擎
        self.graph_engine = GraphEngine(
            graph_store=self.graph_store,
            atom_store=self.atom_store,
            diary_store=self.diary_store,
        )

        # 4. 核心业务模块
        self.capturer = Capturer(
            llm_provider=self.llm_provider,
            diary_store=self.diary_store,
            atom_store=self.atom_store,
            prompts_dir=prompts_dir,
            config=self.config,
            on_atoms_created=self.graph_engine.index_atom,
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

        # 4. 调度器
        self.consolidation_manager = ConsolidationManager(
            capturer=self.capturer,
            persona_engine=self.persona_engine,
            state_store=self.state_store,
            config=self.config,
        )
        await self.consolidation_manager.initialize()

        # 5. WebUI API
        from .page_api import PageApi
        self.page_api = PageApi(self)
        try:
            self.page_api.register_routes(self.plugin_context)
        except Exception as e:
            logger.warning(f"[Memory] 注册 WebUI API 失败: {e}")

        # 6. 指令处理器
        self.command_handler = CommandHandler(
            diary_store=self.diary_store,
            atom_store=self.atom_store,
            persona_store=self.persona_store,
            retriever=self.retriever,
        )

        # 应用 provider 配置
        self._apply_provider_config()

        self._initialized = True

    async def destroy(self):
        """优雅关闭"""
        if self.consolidation_manager:
            await self.consolidation_manager.destroy()
        self._initialized = False

    async def on_message(self, event) -> str | None:
        """
        消息处理入口

        三步走：
        1. 检索相关记忆并注入
        2. 判断是否需要整理
        3. 处理指令

        返回注入后的文本（如果是注入到用户消息前的情况），
        或者 None（系统提示词注入时由插件自己处理）
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
            # 获取系统提示词（从 AstrBot 上下文）
            system_prompt = getattr(event, "system_prompt", "") or ""
            user_message = message_text
            user_name = user_id  # 可以用昵称映射，暂时简单处理

            new_system, new_user = self.injector.inject(
                memory_text=recall_result.memory_text,
                persona_text=recall_result.persona_text,
                system_prompt=system_prompt,
                user_message=user_message,
                user_name=user_name,
            )

            # 如果 system_prompt 变了，更新 event
            if new_system != system_prompt:
                event.system_prompt = new_system

            # 如果 user_message 变了并且是前缀注入，返回修改后的消息
            if new_user != user_message:
                return new_user

        # 2. 异步触发整理（不阻塞对话）
        try:
            import asyncio
            asyncio.ensure_future(
                self.consolidation_manager.on_message(user_id, message_text)
            )
        except Exception:
            pass

        return None

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
            # /日记 带参数的兼容处理
            if cmd == "/日记" and args:
                handler = self.command_handler.handle_diary
            else:
                return

        try:
            result = await handler(user_id, args)
            # 通过 AstrBot API 发送回复
            if hasattr(self.plugin_context, "reply"):
                await self.plugin_context.reply(result)
        except Exception:
            pass

    def reload_config(self, config: dict[str, Any]):
        """热加载配置"""
        self.config.update(config)
        if self.injector:
            self.injector.reload_config(self.config)
        if self.consolidation_manager:
            # 更新调度器配置
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
