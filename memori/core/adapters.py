"""抽象层 — 纯接口定义，零框架依赖

任何 Agent/框架接入 memori 只需实现 LLMProvider、ContextProvider 两个接口。

标准调用链:
    外部事件 → 框架适配层提取 MemoriaEvent
            → context_provider.get_user_id(event)
            → context_provider.get_conversation_text(event)
            → MemoryCore.process_message(...)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


# ═══════════════════════════════════════════════════════════
#  标准事件模型
# ═══════════════════════════════════════════════════════════

@dataclass
class MemoriaEvent:
    """标准化的记忆事件 — 各框架适配层将自有事件转换为此格式

    当 ContextProvider 不方便适配时，可直接构造此对象传入 MemoryCore。

    Attributes:
        user_id:    用户唯一标识（内部 UID，不暴露给 LLM）
        text:       消息原文
        sender_name:发送者昵称（LLM 可见）
        session_id: 会话 ID（用于对话历史检索）
        extra:      框架特定扩展数据
    """

    user_id: str
    text: str
    sender_name: str = ""
    session_id: str = ""
    system_prompt: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════
#  核心接口
# ═══════════════════════════════════════════════════════════

class LLMProvider(ABC):
    """LLM 调用抽象

    接入方需实现 chat() 方法。支持可选的判读/主模型分离。
    """

    @abstractmethod
    async def chat(self, system_prompt: str, user_prompt: str) -> str:
        """调用 LLM，返回回复文本

        Args:
            system_prompt: 系统提示词
            user_prompt:   用户消息

        Returns:
            LLM 回复文本
        """
        ...

    # 可选方法：如果不需要判读/主模型分离，可不重写
    def set_provider(self, provider_id: str | None) -> None:
        """切换主模型（可选实现）"""
        pass

    def set_judge_provider(self, provider_id: str | None) -> None:
        """切换判读模型（可选实现）"""
        pass

    async def chat_with_judge(self, system_prompt: str, user_prompt: str) -> str:
        """用判读模型调用（可选重写，默认等同 chat）"""
        return await self.chat(system_prompt, user_prompt)


class ContextProvider(ABC):
    """事件上下文抽象 — 从不同 Agent 框架提取用户信息

    接入方需实现 get_user_id() 和 get_conversation_text()。
    如果框架事件模型差异大，建议构造 MemoriaEvent 后直接调用
    MemoryCore.process_message()。
    """

    @abstractmethod
    def get_user_id(self, event) -> str:
        """从事件中提取用户唯一标识

        Args:
            event: 框架自有事件对象

        Returns:
            用户 ID 字符串
        """
        ...

    @abstractmethod
    def get_conversation_text(self, event) -> str:
        """从事件中提取对话文本

        Args:
            event: 框架自有事件对象

        Returns:
            消息文本
        """
        ...

    def get_sender_name(self, event) -> str:
        """从事件中提取发送者昵称（可选重写）

        Args:
            event: 框架自有事件对象

        Returns:
            发送者显示名，空字符串则用 user_id 兜底
        """
        return ""


class EmbeddingProvider(ABC):
    """嵌入模型抽象 — 将文本转为向量（可选，默认不启用）

    接入方可实现此接口接入任何嵌入服务（本地 sentence-transformers、OpenAI API 等）。
    传入 MemoryCoreOptions.embed_provider 即可启用向量检索。
    """

    @abstractmethod
    async def embed(self, text: str) -> list[float]:
        """单条文本嵌入

        Args:
            text: 输入文本

        Returns:
            浮点数向量
        """
        ...

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """批量文本嵌入（默认逐条调用，子类可重写为并行）

        Args:
            texts: 输入文本列表

        Returns:
            向量列表，顺序与输入一致
        """
        return [await self.embed(t) for t in texts]

    @property
    @abstractmethod
    def dimension(self) -> int:
        """向量维度"""
        ...

    async def close(self):
        """释放底层资源（可选实现，默认 no-op）

        在 MemoryCore.destroy() 中调用，
        必须先于后台任务的取消，避免正在进行的请求被打断。
        """
        pass

    def set_provider(self, provider_id: str | None) -> None:
        """切换后端（可选实现，默认 no-op）"""
        pass


class MemoryStore(ABC):
    """存储抽象 — 搜索接口，供检索系统扩展

    当前由 memori.storage.atom_store.AtomStore 实现。
    """

    @abstractmethod
    async def search_fts(self, query: str, user_id: str, k: int) -> list:
        """FTS 全文搜索

        Args:
            query:  搜索关键词
            user_id: 用户 ID
            k:        返回数量

        Returns:
            匹配的 MemoryAtom 列表
        """
        ...

    # 预留向量搜索接口
    async def search_vector(self, query: str, user_id: str, k: int) -> list:
        """向量语义搜索（预留）

        接入向量数据库后实现此方法。
        """
        raise NotImplementedError


class CoreUserIdentityResolver:
    """基于 AtomStore 的用户身份解析器 — GraphEngine 跨域解耦

    实现 IUserIdentityResolver 接口，将 user_identities 和 user_persona
    查询从 GraphEngine 中抽离出来。
    """

    def __init__(self, atom_store):
        self._store = atom_store

    async def resolve_display_name(self, display_name: str) -> str | None:
        if not display_name or not self._store:
            return None
        try:
            row = await self._store.fetchone(
                "SELECT uid FROM user_identities WHERE display_name=? LIMIT 1",
                (display_name,),
            )
            return row[0] if row else None
        except Exception:
            return None

    async def get_persona_summary(self, uid: str) -> str:
        if not uid or not self._store:
            return ""
        try:
            row = await self._store.fetchone(
                "SELECT summary FROM user_persona WHERE uid=?", (uid,)
            )
            return row[0] if row else ""
        except Exception:
            return ""

    async def get_persona_full(self, display_name: str) -> dict | None:
        if not display_name or not self._store:
            return None
        try:
            uid_row = await self._store.fetchone(
                "SELECT uid FROM user_identities WHERE display_name=? LIMIT 1",
                (display_name,),
            )
            if not uid_row:
                return None
            uid = uid_row[0]
            persona = await self._store.fetchone(
                "SELECT summary, tags, tier, primary_name FROM user_persona WHERE uid=?",
                (uid,),
            )
            if not persona:
                return {"uid": uid, "summary": "", "tags": [], "tier": "new"}
            import json
            tags = []
            if persona[1]:
                try:
                    tags = json.loads(persona[1]) if isinstance(persona[1], str) else persona[1]
                except Exception:
                    tags = []
            return {
                "uid": uid,
                "summary": persona[0] or "",
                "tags": tags,
                "tier": persona[2] or "new",
                "name": persona[3] or display_name,
            }
        except Exception:
            return None
