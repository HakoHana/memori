"""AstrBot 框架适配器

适配器类:
    AstrBotLLM      — 将 AstrBot LLM Provider 包装为 memori.LLMProvider
    AstrBotCtx      — 从 AstrBot 事件中提取用户信息

Star 插件:
    MemoriPlugin    — @register 注册的 AstrBot Star 插件

Agent Tools:
    RecallTool      — 搜索长期记忆的 FunctionTool
    MemorizeTool    — 主动写入记忆的 FunctionTool
"""

from .plugin import MemoriPlugin
from .adapter import AstrBotLLM, AstrBotCtx
from .tools import RecallTool, MemorizeTool

__all__ = [
    "MemoriPlugin",
    "AstrBotLLM",
    "AstrBotCtx",
    "RecallTool",
    "MemorizeTool",
]
