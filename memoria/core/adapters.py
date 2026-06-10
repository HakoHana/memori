"""抽象层 — 纯接口定义，零框架依赖

任何 Agent/框架接入 memoria 只需实现这三个接口。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class LLMProvider(ABC):
    """LLM 调用抽象"""

    @abstractmethod
    async def chat(self, system_prompt: str, user_prompt: str) -> str:
        """调用 LLM，返回回复文本"""
        ...


class MemoryStore(ABC):
    """存储抽象 — 目前由 AtomStore 实现"""

    @abstractmethod
    async def search_fts(self, query: str, user_id: str, k: int) -> list:
        """FTS 全文搜索"""
        ...

    # 预留向量搜索接口
    async def search_vector(self, query: str, user_id: str, k: int) -> list:
        """向量语义搜索（预留）"""
        raise NotImplementedError


class ContextProvider(ABC):
    """事件上下文抽象 — 从不同 Agent 框架提取用户信息"""

    @abstractmethod
    def get_user_id(self, event) -> str:
        """从事件中提取用户 ID"""
        ...

    @abstractmethod
    def get_conversation_text(self, event) -> str:
        """获取对话文本"""
        ...
