"""FastAPI 依赖注入 — 从应用状态获取核心实例"""

from __future__ import annotations

from fastapi import Request

from ..core.memory_core import MemoryCore


def get_core(request: Request) -> MemoryCore:
    """从 FastAPI app.state 获取 MemoryCore 实例"""
    core: MemoryCore | None = getattr(request.app.state, "memory_core", None)
    if core is None:
        raise RuntimeError("MemoryCore 未初始化，请先调用 app.state.memory_core = core")
    return core
