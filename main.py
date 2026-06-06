"""AstrBot Memory Plugin — 日记式长期记忆插件"""

from __future__ import annotations

import asyncio

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api import logger

from .core.memory_core import MemoryCore
from .core.memory_tools import RecallMemoryTool, MemorizeMemoryTool


@register(
    name="Memory",
    author="your_name",
    desc="日记式长期记忆插件 — 让 Bot 记住与用户的每一刻",
    version="0.2.0",
    repo="https://github.com/your_name/astrbot_plugin_memory",
)
class MemoryPlugin(Star):
    """记忆插件主入口"""

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.memory_core: MemoryCore | None = None

    async def initialize(self):
        data_dir = str(StarTools.get_data_dir())
        logger.info(f"[Memory] 初始化: {data_dir}")
        self.memory_core = MemoryCore(
            plugin_context=self.context,
            data_dir=data_dir,
            config=self.config,
        )
        await self.memory_core.initialize()
        try:
            recall_tool = RecallMemoryTool()
            recall_tool.set_memory_core(self.memory_core)
            memorize_tool = MemorizeMemoryTool()
            memorize_tool.set_memory_core(self.memory_core)
            self.context.add_llm_tools(recall_tool, memorize_tool)
            logger.info("[Memory] Agent Tools 已注册")
        except Exception as e:
            logger.warning(f"[Memory] 注册 Agent Tools 失败: {e}")
        logger.info("[Memory] 初始化完成")

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.memory_core:
            return
        try:
            # 如果是指令已在 on_message 处理过，阻止 LLM
            raw_text = event.get_message_str() if hasattr(event, 'get_message_str') else str(event.message_str)
            if raw_text.startswith("/"):
                if hasattr(req, 'prompt'):
                    req.prompt = None
                if req.contexts:
                    req.contexts.clear()
                event.message_str = ""
                if hasattr(event, 'message_obj') and event.message_obj:
                    event.message_obj.message_str = ""
                return

            # 存储用户消息到会话
            cs = self.memory_core.conversation_store
            if cs and raw_text:
                sid = await cs.get_session_id(event)
                uid = await cs.get_user_id(event)
                await cs.add_message(sid, uid, "user", raw_text)

            # 记忆注入
            result = await self.memory_core.on_message(event)
            if result is not None:
                event.message_obj.message_str = result
        except Exception as e:
            logger.error(f"[Memory] on_llm_request 出错: {e}")

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        if not self.memory_core:
            return
        try:
            uid = self.memory_core.context_provider.get_user_id(event)
            txt = self.memory_core.context_provider.get_conversation_text(event)

            # 检测指令 → 在 LLM 处理前拦截，直接回复
            if txt and txt.startswith("/"):
                await self.memory_core._handle_command(uid, txt)
                event.message_str = ""
                if hasattr(event, 'message_obj') and event.message_obj:
                    event.message_obj.message_str = ""
                return

            if uid and txt:
                logger.debug(f"[Memory] on_message: {uid}")
                await self.memory_core.consolidation_manager.on_message(uid, txt)
        except Exception as e:
            logger.error(f"[Memory] on_message 出错: {e}")

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response: LLMResponse = None):
        if not self.memory_core:
            return
        try:
            # 存储 Bot 回复
            cs = self.memory_core.conversation_store
            if cs and response:
                sid = await cs.get_session_id(event)
                uid = await cs.get_user_id(event)
                resp_text = ""
                if hasattr(response, "result_chain") and response.result_chain:
                    resp_text = response.result_chain.get_plain_text() or ""
                if resp_text:
                    await cs.add_message(sid, uid, "assistant", resp_text)

            # 后台触发记忆整理
            user_id = self.memory_core.context_provider.get_user_id(event)
            text = self.memory_core.context_provider.get_conversation_text(event)
            if user_id and text and self.memory_core:
                logger.debug(f"[Memory] on_response 触发整理: {user_id}")
                task = asyncio.ensure_future(
                    self.memory_core.consolidation_manager.on_message(user_id, text)
                )
                self.memory_core._background_tasks.add(task)
                task.add_done_callback(self.memory_core._background_tasks.discard)
        except Exception as e:
            logger.error(f"[Memory] on_response 出错: {e}")

    async def on_unload(self):
        if self.memory_core:
            await self.memory_core.destroy()
            # 解释器关闭阶段可能会触发 threading._shutdown 错误，
            # 同步关闭连接池确保后台线程释放
            try:
                from .storage.base_store import BaseDbStore
                BaseDbStore.close_all_sync()
            except Exception:
                pass
            logger.info("[Memory] 已卸载")
