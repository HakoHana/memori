"""Memoria — 长期记忆内核

纯净的 Python 包，无任何框架依赖。
通过 adapters 模块的接口接入各种 Agent/框架。

快速开始:
    from memoria import MemoryCore
    from memoria.core.adapters import LLMProvider, ContextProvider

    class MyLLM(LLMProvider): ...
    class MyContext(ContextProvider): ...

    core = MemoryCore(llm_provider=MyLLM(), context_provider=MyContext())
    await core.initialize()
    await core.process_message(user_id="...", message_text="...")
"""

from .core.memory_core import MemoryCore
from .core.adapters import LLMProvider, ContextProvider, MemoryStore
from .models.memory_atom import MemoryAtom, AtomType, AtomStatus, RecallResult
from .core.retriever import Retriever
from .core.hot_cache import HotMessageCache
from .core.memory_injector import MemoryInjector

__all__ = [
    # 核心入口
    "MemoryCore",
    # 抽象接口（框架接入需实现）
    "LLMProvider", "ContextProvider", "MemoryStore",
    # 数据模型
    "MemoryAtom", "AtomType", "AtomStatus", "RecallResult",
    # 子模块
    "Retriever", "HotMessageCache", "MemoryInjector",
]

__version__ = "0.1.0"
