"""AstrBot 适配器 — LLM Provider + Context Provider 实现

将 AstrBot 的 LLM Provider 和事件体系包装为 memori 核心接口，
使 memori 内核不感知 AstrBot 的存在。
"""

from __future__ import annotations

from astrbot.api import logger
from astrbot.api.star import Context

from ...memori.core.adapters import LLMProvider, ContextProvider


class AstrBotLLM(LLMProvider):
    """将 AstrBot 的 LLM Provider 包装为 memori.LLMProvider

    支持主模型/判读模型分离：
    - chat()              使用 AstrBot 当前主 LLM
    - chat_with_judge()   使用 AstrBot 的判读模型（如单独配置）
    - set_provider()      切换主模型 ID
    - set_judge_provider() 切换判读模型 ID
    """

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
    """从 AstrBot 事件中提取用户信息

    兼容 AstrMessageEvent 的多种版本：
    - get_sender_id() / get_session_id()（新版本 API）
    - sender.user_id / group_id（旧版本兼容）
    """

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
