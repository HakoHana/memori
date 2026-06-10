"""AstrBot 适配插件 — 将 AstrBot 的 LLM/事件连接至 memori 内核"""

from __future__ import annotations

import asyncio

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api import logger

from memori import MemoryCore
from memori.core.adapters import LLMProvider, ContextProvider


# ═══════════════════════════════════════════════════════════
#  AstrBot 适配器实现
# ═══════════════════════════════════════════════════════════

class AstrBotLLM(LLMProvider):
    """将 AstrBot 的 LLM Provider 包装为 memori.LLMProvider"""

    def __init__(self, context: Context):
        self.context = context
        self._provider = None
        self._provider_id = None
        self._judge_provider = None
        self._judge_provider_id = None

    def set_provider(self, provider_id: str | None) -> None:
        self._provider_id = provider_id
        self._provider = None

    def set_judge_provider(self, provider_id: str | None) -> None:
        self._judge_provider_id = provider_id
        self._judge_provider = None

    def _get_provider(self, use_judge: bool = False):
        key = "_judge_provider" if use_judge else "_provider"
        pid = "_judge_provider_id" if use_judge else "_provider_id"
        pid_val = getattr(self, pid, None)
        cached = getattr(self, key, None)
        if cached:
            return cached
        if pid_val:
            try:
                p = self.context.get_provider_by_id(pid_val)
                if p:
                    setattr(self, key, p)
                    return p
            except Exception:
                pass
        p = self.context.get_using_provider()
        setattr(self, key, p)
        return p

    async def chat(self, system_prompt: str, user_prompt: str) -> str:
        provider = self._get_provider(use_judge=False)
        if not provider:
            raise RuntimeError("没有可用的 LLM Provider")
        result = await provider.text_chat(
            prompt=user_prompt,
            system_prompt=system_prompt,
        )
        if hasattr(result, "result_chain") and result.result_chain:
            return result.result_chain.get_plain_text() or ""
        if hasattr(result, "completion"):
            return result.completion
        if hasattr(result, "text"):
            return result.text
        return str(result)

    async def chat_with_judge(self, system_prompt: str, user_prompt: str) -> str:
        provider = self._get_provider(use_judge=True)
        if not provider:
            return await self.chat(system_prompt, user_prompt)
        result = await provider.text_chat(
            prompt=user_prompt,
            system_prompt=system_prompt,
        )
        if hasattr(result, "result_chain") and result.result_chain:
            return result.result_chain.get_plain_text() or ""
        if hasattr(result, "completion"):
            return result.completion
        if hasattr(result, "text"):
            return result.text
        return str(result)


class AstrBotCtx(ContextProvider):
    """从 AstrBot 事件中提取用户信息"""

    def get_user_id(self, event) -> str:
        try:
            if hasattr(event, "get_sender_id"):
                sid = event.get_sender_id()
                if sid:
                    return str(sid)
        except Exception:
            pass
        try:
            if hasattr(event, "get_session_id"):
                return str(event.get_session_id())
        except Exception:
            pass
        sender = getattr(event, "sender", None)
        if sender:
            uid = getattr(sender, "user_id", None)
            if uid:
                return str(uid)
        gid = getattr(event, "group_id", None)
        if gid:
            return f"group_{gid}"
        return "default"

    def get_conversation_text(self, event) -> str:
        if hasattr(event, "get_message_str"):
            return event.get_message_str() or ""
        if hasattr(event, "message_str"):
            return event.message_str or ""
        return getattr(event, "message", "") or ""

    def get_sender_name(self, event) -> str:
        try:
            if hasattr(event, "get_sender_name"):
                name = event.get_sender_name()
                if name:
                    return str(name)
        except Exception:
            pass
        if hasattr(event, "sender_name"):
            name = event.sender_name
            if name:
                return str(name)
        if hasattr(event, "message_obj") and event.message_obj:
            sender = getattr(event.message_obj, "sender", None)
            if sender:
                for attr in ("card", "nickname", "name", "user_displayname"):
                    val = getattr(sender, attr, None)
                    if val:
                        return str(val)
        return ""


# ═══════════════════════════════════════════════════════════
#  AstrBot 插件入口
# ═══════════════════════════════════════════════════════════

@register(
    name="memori",
    author="HakoHana",
    desc="长期记忆插件 — 基于 memori 内核",
    version="0.1.0",
    repo="https://github.com/HakoHana/memori",
)
class MemoriPlugin(Star):
    """将 memori 长期记忆内核接入 AstrBot"""

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.core: MemoryCore | None = None

    async def initialize(self):
        data_dir = str(StarTools.get_data_dir())
        logger.info(f"[memori] 初始化内核: {data_dir}")

        # 构造适配器
        llm = AstrBotLLM(self.context)
        ctx = AstrBotCtx()

        self.core = MemoryCore(
            config=self.config,
            llm_provider=llm,
            context_provider=ctx,
            data_dir=data_dir,
        )
        await self.core.initialize()

        # 注册 Agent 工具
        try:
            from .tools import RecallTool, MemorizeTool
            recall_tool = RecallTool()
            recall_tool.set_core(self.core)
            memorize_tool = MemorizeTool()
            memorize_tool.set_core(self.core)
            self.context.add_llm_tools(recall_tool, memorize_tool)
            self.context.activate_llm_tool("recall_long_term_memory")
            self.context.activate_llm_tool("memorize_long_term_memory")
            logger.info("[memori] Agent Tools 已注册")
        except Exception as e:
            logger.warning(f"[memori] 注册 Agent Tools 失败: {e}")

        logger.info("[memori] 内核就绪")

    def _get_sender_name(self, event) -> str:
        """提取发送者昵称"""
        try:
            return AstrBotCtx().get_sender_name(event)
        except Exception:
            return ""

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.core:
            return

        raw_text = event.get_message_str() if hasattr(event, 'get_message_str') else str(event.message_str)
        if not raw_text or raw_text.startswith("/"):
            if hasattr(req, 'prompt'):
                req.prompt = None
            if req.contexts:
                req.contexts.clear()
            if hasattr(event, 'message_str'):
                event.message_str = ""
            return

        uid = AstrBotCtx().get_user_id(event)
        sender_name = self._get_sender_name(event)

        # 注册用户身份
        if self.core.atom_store and uid:
            try:
                await self.core.atom_store.ensure_user(uid, sender_name)
            except Exception:
                pass
            try:
                await self.core.atom_store.ensure_canonical_user(f"qq:{uid}", sender_name, "qq")
            except Exception:
                pass

        # 存储到会话
        cs = self.core.conversation_store
        if cs and raw_text:
            sid = await cs.get_session_id(event)
            await cs.add_message(sid, uid, "user", raw_text, sender_name)

        # 记忆注入
        system_prompt = getattr(event, "system_prompt", "") or ""
        result = await self.core.process_message(
            user_id=uid,
            message_text=raw_text,
            sender_name=sender_name,
            system_prompt=system_prompt,
        )

        if result is not None:
            event.message_obj.message_str = result

        if hasattr(event, 'system_prompt') and event.system_prompt and req:
            req.system_prompt = event.system_prompt

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response: LLMResponse = None):
        if not self.core:
            return
        try:
            cs = self.core.conversation_store
            if cs and response:
                sid = await cs.get_session_id(event)
                uid = AstrBotCtx().get_user_id(event)
                resp_text = ""
                if hasattr(response, "result_chain") and response.result_chain:
                    resp_text = response.result_chain.get_plain_text() or ""
                if resp_text:
                    bot_name = self.config.get("bot_name", "Hana")
                    await cs.add_message(sid, uid, "assistant", resp_text, bot_name)
        except Exception as e:
            logger.error(f"[memori] on_response 出错: {e}")

    async def on_unload(self):
        if self.core:
            await self.core.destroy()
            try:
                from memori.storage.base_store import BaseDbStore
                BaseDbStore.close_all_sync()
            except Exception:
                pass
            logger.info("[memori] 内核已关闭")
