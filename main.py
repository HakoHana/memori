"""AstrBot Memory Plugin — 日记式长期记忆插件"""

from __future__ import annotations

import asyncio

from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api import logger

from .core.memory_core import MemoryCore


@register(
    name="Memory",
    author="your_name",
    desc="日记式长期记忆插件 — 让 Bot 记住与用户的每一刻",
    version="0.1.0",
    repo="https://github.com/your_name/astrbot_plugin_memory",
)
class MemoryPlugin(Star):
    """记忆插件主入口"""

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.memory_core: MemoryCore | None = None

    async def initialize(self):
        """插件初始化"""
        data_dir = str(StarTools.get_data_dir())
        logger.info(f"[Memory] 初始化中，数据目录: {data_dir}")

        self.memory_core = MemoryCore(
            plugin_context=self.context,
            data_dir=data_dir,
            config=self.config,
        )
        await self.memory_core.initialize()
        logger.info("[Memory] 初始化完成！")

    async def on_astrbot_llm_request(self, event: AstrMessageEvent):
        """在 LLM 请求前注入记忆（兼容 AstrBot 新版事件）"""
        if not self.memory_core:
            return

        try:
            result = await self.memory_core.on_message(event)
            # 如果注入器修改了用户消息，更新 event
            if result is not None:
                event.message_obj.message_str = result
        except Exception as e:
            logger.error(f"[Memory] on_astrbot_llm_request 错误: {e}")

    async def on_llm_request(self, event: AstrMessageEvent):
        """兼容旧版事件"""
        await self.on_astrbot_llm_request(event)

    async def on_message(self, event: AstrMessageEvent):
        """消息回调 — 仅计数，不触发整理"""
        if self.memory_core:
            try:
                user_id = self.memory_core.context_provider.get_user_id(event)
                if user_id:
                    await self.memory_core.consolidation_manager.on_message(user_id, "")
            except Exception:
                pass

    async def on_llm_response(self, event: AstrMessageEvent):
        """LLM 响应后 — 触发记忆整理"""
        if self.memory_core:
            try:
                # 使用 LLM 响应后的完整上下文触发整理
                user_id = self.memory_core.context_provider.get_user_id(event)
                if user_id:
                    text = self.memory_core.context_provider.get_conversation_text(event)
                    asyncio.ensure_future(
                        self.memory_core.consolidation_manager.on_message(user_id, text)
                    )
            except Exception as e:
                logger.error(f"[Memory] on_llm_response error: {e}")

    async def on_unload(self):
        """插件卸载时清理"""
        if self.memory_core:
            await self.memory_core.destroy()
            logger.info("[Memory] 已关闭")
