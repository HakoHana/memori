"""FastAPI 应用工厂 — 将 MemoryCore 包装为 RESTful API 服务"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ..core.memory_core import MemoryCore
from .routes import router


class SimpleProvider:
    """最小 LLMProvider — 用于独立服务模式（由上层接入方替换）"""

    def __init__(self):
        self._provider_id = None
        self._judge_id = None

    async def chat(self, system_prompt: str, user_prompt: str) -> str:
        raise NotImplementedError(
            "请提供真实的 LLMProvider 实现: "
            "app.state.memory_core.llm_provider = MyProvider()"
        )

    def set_provider(self, pid: str | None): self._provider_id = pid
    def set_judge_provider(self, pid: str | None): self._judge_id = pid


class SimpleContext:
    """最小 ContextProvider — 纯 API 模式下直接使用 user_id/text"""

    def get_user_id(self, event) -> str:
        return getattr(event, "user_id", "default")

    def get_conversation_text(self, event) -> str:
        return getattr(event, "text", "")

    def get_sender_name(self, event) -> str:
        return getattr(event, "sender_name", "")


def create_app(
    memory_core: MemoryCore | None = None,
    config: dict[str, Any] | None = None,
    data_dir: str | None = None,
    **kwargs,
) -> FastAPI:
    """创建 FastAPI 应用实例

    Args:
        memory_core: 已初始化的 MemoryCore 实例（优先）
        config:      如果未传入 core 则创建新实例时使用
        data_dir:    数据目录
        **kwargs:    传递给 MemoryCore 的额外参数
    """
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 启动：创建并初始化 MemoryCore
        core = getattr(app.state, "memory_core", None)
        if core is None:
            core = MemoryCore(
                config=config or {},
                llm_provider=SimpleProvider(),
                context_provider=SimpleContext(),
                data_dir=data_dir or str(Path.cwd() / "data" / "memoria"),
                **kwargs,
            )
            app.state.memory_core = core
        if not core._initialized:
            await core.initialize()
        yield
        # 关闭
        await core.destroy()
        app.state.memory_core = None

    app = FastAPI(
        title="Memoria Memory API",
        version="0.1.0",
        description="长期记忆内核 RESTful API — 日记/原子/图谱/画像",
        lifespan=lifespan,
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=kwargs.get("cors_origins", ["*"]),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 如果已有 core，直接挂载
    if memory_core is not None:
        app.state.memory_core = memory_core

    # 路由
    app.include_router(router, prefix="/api")

    # 健康检查
    @app.get("/health")
    async def health():
        core = getattr(app.state, "memory_core", None)
        return {
            "status": "ok" if core and core._initialized else "starting",
            "version": "0.1.0",
        }

    # 全局错误处理
    @app.exception_handler(Exception)
    async def global_exception(request: Request, exc: Exception):
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(exc)},
        )

    return app
