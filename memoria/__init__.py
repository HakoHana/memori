"""Memoria — 长期记忆内核

纯净的 Python 包，无任何框架依赖。
通过 adapters.LLMProvider / ContextProvider 接口接入各种 Agent。
"""

from .core.memory_core import MemoryCore
from .core.adapters import LLMProvider, ContextProvider, MemoryStore

__all__ = ["MemoryCore", "LLMProvider", "ContextProvider", "MemoryStore"]
__version__ = "0.1.0"
