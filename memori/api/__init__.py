"""FastAPI 应用工厂 — 将 MemoryCore 包装为 RESTful API 服务"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..core.memory_core import MemoryCore
from .routes import router

_CONFIG_FILE = "memori_config.json"


class BuiltinLLMProvider:
    """memori 内置 LLM Provider — 零外部依赖，直接调用 API

    从 memori 自己的配置中读取提供商信息（api_base / api_key / model）。
    支持 OpenAI 兼容接口（OpenAI / Anthropic / vLLM / Ollama 等）。
    """

    def __init__(self):
        self._config_ref = None       # 指向 MemoryCore.config
        self._provider_id = None      # 当前选中的提供商名称
        self._judge_id = None         # 判读模型提供商名称

    def bind_config(self, config: dict):
        """绑定到 MemoryCore 的配置字典"""
        self._config_ref = config

    def set_provider(self, pid: str | None):
        self._provider_id = pid

    def set_judge_provider(self, pid: str | None):
        self._judge_id = pid

    def _get_provider_cfg(self, name: str) -> dict | None:
        """从配置中查找指定名称的提供商"""
        if not self._config_ref or not name:
            return None
        for p in self._config_ref.get("_providers", []):
            if p.get("name") == name:
                return p
        return None

    async def chat(self, system_prompt: str, user_prompt: str) -> str:
        return await self._call_llm(self._provider_id, system_prompt, user_prompt)

    async def chat_with_judge(self, system_prompt: str, user_prompt: str) -> str:
        pid = self._judge_id or self._provider_id
        return await self._call_llm(pid, system_prompt, user_prompt)

    async def _call_llm(self, provider_name: str | None, system: str, user: str) -> str:
        cfg = self._get_provider_cfg(provider_name)
        if not cfg:
            raise RuntimeError(
                f"LLM 提供商「{provider_name}」未配置。"
                f"请在 WebUI 配置页 → 模型提供商 中添加。"
            )

        api_base = cfg.get("api_base", "").rstrip("/")
        api_key = cfg.get("api_key", "")
        model = cfg.get("model", "")

        if not api_base or not api_key:
            raise RuntimeError(f"提供商「{provider_name}」缺少 API 地址或 Key")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model or "gpt-4o",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": 2048,
        }

        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    f"{api_base}/chat/completions",
                    headers=headers,
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"] or ""
        except httpx.TimeoutException:
            raise RuntimeError(f"LLM 调用超时（{provider_name}）")
        except httpx.HTTPStatusError as e:
            detail = ""
            try:
                detail = e.response.text[:200]
            except Exception:
                pass
            raise RuntimeError(f"LLM API 错误 ({provider_name}): {e.response.status_code} {detail}")
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"LLM 返回格式异常 ({provider_name}): {e}")


class SimpleContext:
    """最小 ContextProvider — 纯 API 模式下直接使用 user_id/text"""

    def get_user_id(self, event) -> str:
        return getattr(event, "user_id", "default")

    def get_conversation_text(self, event) -> str:
        return getattr(event, "text", "")

    def get_sender_name(self, event) -> str:
        return getattr(event, "sender_name", "")


def _load_config(data_dir: str) -> dict:
    """从 JSON 文件加载持久化配置"""
    path = Path(data_dir) / _CONFIG_FILE
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[memori] 配置加载失败: {e}")
    return {}


def _save_config(data_dir: str, config: dict) -> None:
    """保存配置到 JSON 文件"""
    path = Path(data_dir) / _CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"[memori] 配置保存失败: {e}")


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
        data_dir:    数据目录，配置 JSON 存放在此
    """
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        resolved_data_dir = data_dir or str(Path.cwd() / "data" / "memori")
        app.state._data_dir = resolved_data_dir

        # 从持久化加载配置
        persisted = _load_config(resolved_data_dir)
        merged_config = {**(config or {}), **persisted}

        core = getattr(app.state, "memory_core", None)
        if core is None:
            # 创建内置 LLM Provider 并绑定配置
            llm = BuiltinLLMProvider()
            llm.bind_config(merged_config)

            core = MemoryCore(
                config=merged_config,
                llm_provider=llm,
                context_provider=SimpleContext(),
                data_dir=resolved_data_dir,
                **kwargs,
            )
            app.state.memory_core = core
        elif persisted:
            core.config.update(persisted)
            core.reload_config(core.config)

        if not core._initialized:
            await core.initialize()
            # 初始化后把配置里的模型选择推给 provider
            core.reload_config(core.config)

        yield

        # 关闭时保存配置
        if hasattr(app.state, "memory_core") and app.state.memory_core:
            _save_config(resolved_data_dir, app.state.memory_core.config)
        await core.destroy()
        app.state.memory_core = None

    app = FastAPI(
        title="Memori Memory API",
        version="0.1.0",
        description="长期记忆内核 RESTful API — 日记/原子/图谱/画像",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=kwargs.get("cors_origins", ["*"]),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    if memory_core is not None:
        app.state.memory_core = memory_core

    app.include_router(router, prefix="/api")

    # WebUI 静态文件（JS/CSS 用 StaticFiles，HTML 手动注入配置数据）
    _webui_path = Path(__file__).parent.parent.parent / "webui"
    if _webui_path.exists():
        from fastapi.responses import FileResponse

        @app.get("/webui/dashboard/index.html")
        async def dashboard_page(core: MemoryCore = Depends(get_core)):
            import json
            from .routes import _CONFIG_META
            # 构建配置数据
            groups = {}
            for key, meta in _CONFIG_META.items():
                group = meta["group"]
                if group not in groups:
                    groups[group] = []
                if key.startswith("archive_"):
                    sub_key = key.replace("archive_", "")
                    archive_cfg = core.config.get("archive", {})
                    current = archive_cfg.get(sub_key, meta["default"])
                else:
                    current = core.config.get(key, meta["default"])
                groups[group].append({"key": key, "value": current, **meta})
            config_json = json.dumps({"groups": groups}, ensure_ascii=False)

            html = (_webui_path / "dashboard" / "index.html").read_text(encoding="utf-8")
            # 在 </head> 前注入配置数据
            script = f'<script>window.__MEMORI_CONFIG__ = {config_json};</script>'
            html = html.replace("</head>", script + "</head>")
            return HTMLResponse(content=html)

        app.mount("/webui", StaticFiles(directory=str(_webui_path)), name="webui")

    _ui_path = Path(__file__).parent / "webui_config.html"

    @app.get("/config")
    async def config_page():
        return HTMLResponse(content=_ui_path.read_text(encoding="utf-8"))

    @app.get("/health")
    async def health():
        core = getattr(app.state, "memory_core", None)
        return {
            "status": "ok" if core and core._initialized else "starting",
            "version": "0.1.0",
        }

    @app.exception_handler(Exception)
    async def global_exception(request: Request, exc: Exception):
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(exc)},
        )

    return app
