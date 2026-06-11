"""memori — 长期记忆内核

此包提供核心 API：
    MemoryCore        统一门面
    LLMProvider       大模型抽象接口
    ContextProvider   事件上下文抽象接口

Agent 框架适配器请见 adapters/ 目录：
    adapters/astrbot/          AstrBot 插件
    adapters/platform_tools.py 通用 Agent Tool（LangChain / OpenAI）
"""

from .memori import MemoryCore
from .memori.core.adapters import LLMProvider, ContextProvider

__all__ = ["MemoryCore", "LLMProvider", "ContextProvider"]
