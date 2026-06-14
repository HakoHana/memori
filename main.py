"""memori — AstrBot Star 插件入口

AstrBot 默认导入 main.py 查找 Star 子类，插件类必须定义在此文件。
适配器辅助类（AstrBotLLM / AstrBotCtx / Agent Tools）位于 adapters/astrbot/。
"""

from __future__ import annotations

import asyncio
import sys
import time
import warnings

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api import logger

from .memori import MemoryCore
from .adapters.astrbot.adapter import AstrBotLLM, AstrBotCtx
from .adapters.astrbot.tools import RecallTool, MemorizeTool


@register(
    name="memori",
    author="HakoHana",
    desc="长期记忆插件 — 基于 memori 内核，自动提取、存储、检索对话记忆",
    version="0.2.0",
    repo="https://github.com/HakoHana/memori",
)
class MemoriPlugin(Star):
    """将 memori 长期记忆内核接入 AstrBot"""

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.core: MemoryCore | None = None
        self._http_server = None

    async def initialize(self):
        data_dir = str(StarTools.get_data_dir())
        logger.info(f"[memori] 初始化内核: {data_dir}")

        llm = AstrBotLLM(self.context)
        ctx = AstrBotCtx()

        self.core = MemoryCore(
            config=self.config,
            llm_provider=llm,
            context_provider=ctx,
            data_dir=data_dir,
        )
        await self.core.initialize()

        # 从 AstrBot 同步 LLM 提供商配置
        try:
            pm = getattr(self.context, "provider_manager", None)
            if pm and hasattr(pm, "providers_config"):
                astrbot_providers = []
                for pc in pm.providers_config:
                    keys = pc.get("key", [])
                    api_key = keys[0] if isinstance(keys, list) and keys else ""
                    astrbot_providers.append({
                        "name": pc.get("id", ""),
                        "api_base": pc.get("api_base", ""),
                        "api_key": api_key,
                        "model": pc.get("model", "") or "",
                    })
                if astrbot_providers:
                    self.core.config.setdefault("_providers", [])
                    existing = {p["name"] for p in self.core.config["_providers"]}
                    for p in astrbot_providers:
                        if p["name"] in existing:
                            for i, ep in enumerate(self.core.config["_providers"]):
                                if ep["name"] == p["name"]:
                                    self.core.config["_providers"][i] = p
                                    break
                        else:
                            self.core.config["_providers"].append(p)
                    self.core.reload_config(self.core.config)
                    logger.info(f"[memori] 已同步 {len(astrbot_providers)} 个 LLM 提供商")
        except Exception as e:
            logger.warning(f"[memori] 同步 LLM 提供商失败: {e}")

        # 注册 Agent Tools（受配置开关控制）
        try:
            tools = []
            if self.config.get("agent_recall_tool_enabled", True):
                recall_tool = RecallTool()
                recall_tool.set_core(self.core)
                tools.append(recall_tool)
                self.context.activate_llm_tool("recall_long_term_memory")
            if self.config.get("agent_memorize_tool_enabled", False):
                memorize_tool = MemorizeTool()
                memorize_tool.set_core(self.core)
                tools.append(memorize_tool)
                self.context.activate_llm_tool("memorize_long_term_memory")
            if tools:
                self.context.add_llm_tools(*tools)
                logger.info(f"[memori] Agent Tools 已注册: {len(tools)} 个")
        except Exception as e:
            logger.warning(f"[memori] 注册 Agent Tools 失败: {e}")

        logger.info("[memori] 内核就绪")

        # 启动 HTTP 服务（后台，提供 Dashboard + API）
        await self._start_http_server()

    async def _start_http_server(self):
        """启动 FastAPI 后台服务，提供 Dashboard 和 REST API"""
        api_port = int(self.config.get("api_port", 8765))
        api_host = self.config.get("api_host", "127.0.0.1")

        # 检查 FastAPI / Uvicorn 是否可用（可能不在 AstrBot 的 uv 环境中）
        try:
            from .memori.api import create_app
            import uvicorn
        except ImportError as e:
            logger.warning(
                f"[memori] HTTP 服务跳过: 缺少依赖（{e}）。\n"
                f"  Dashboard → 安装依赖后可用: pip install 'memori[server]'\n"
                f"  或独立运行: python -m memori --port {api_port}"
            )
            return

        # 等待端口释放（插件重载时旧 socket 可能还没关完）
        for attempt in range(10):
            if not await self._is_port_in_use(api_host, api_port):
                break
            if attempt == 0:
                logger.info(f"[memori] 等待端口 {api_port} 释放...")
            await asyncio.sleep(0.3)
        else:
            logger.warning(f"[memori] 端口 {api_port} 一直被占用，跳过 HTTP 服务")
            return

        try:
            app = create_app(memory_core=self.core)
            cfg = uvicorn.Config(
                app=app,
                host=api_host,
                port=api_port,
                log_level="warning",
            )
            server = uvicorn.Server(cfg)
            self._http_server_obj = server

            # 安全包装：兜底 uvicorn 的 sys.exit(1)，防止意外崩掉 AstrBot
            async def _serve_safe():
                try:
                    await server.serve()
                except SystemExit:
                    pass

            self._http_server = asyncio.ensure_future(_serve_safe())
            logger.info(f"[memori] HTTP 服务已启动: http://{api_host}:{api_port}")
        except Exception as e:
            logger.warning(f"[memori] HTTP 服务启动失败: {e}")

    @staticmethod
    async def _is_port_in_use(host: str, port: int) -> bool:
        """检查端口是否被占用"""
        try:
            import socket
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=1
            )
            writer.close()
            await writer.wait_closed()
            return True
        except (ConnectionRefusedError, OSError, asyncio.TimeoutError):
            return False

    def _get_sender_name(self, event) -> str:
        try:
            name = AstrBotCtx().get_sender_name(event)
            if name:
                return name
        except Exception:
            pass
        return ""

    async def _ensure_user_identity(self, user_id: str, event_sender_name: str = "") -> tuple[str, str]:
        """查/建 canonical uid → 返回 (canonical_uid, display_name)

        所有内部存储操作必须使用 canonical_uid，显示名只对 LLM/用户展示用。
        """
        if not self.core or not self.core.atom_store:
            display = event_sender_name or (f"用户{user_id[-4:]}" if len(user_id) >= 4 else "用户")
            return (user_id, display)

        try:
            platform_id = f"qq:{user_id}"
            result = await self.core.atom_store.resolve_identity(platform_id)

            if result:
                cuid, name = result
                if not name:
                    name = event_sender_name or (f"用户{user_id[-4:]}" if len(user_id) >= 4 else "用户")
                # 更新最后活跃
                await self.core.atom_store.execute(
                    "UPDATE user_identities SET display_name=?, last_seen=? WHERE platform_id=?",
                    (name, time.time(), platform_id),
                )
                return (cuid, name)

            # 新用户
            import uuid
            cuid = "u_" + uuid.uuid4().hex[:12]
            now = time.time()
            name = event_sender_name.strip() if event_sender_name and event_sender_name.strip() else (
                f"用户{user_id[-4:]}" if len(user_id) >= 4 else "用户"
            )
            await self.core.atom_store.execute(
                "INSERT INTO canonical_users (uid, primary_name, created_at, updated_at) VALUES (?,?,?,?)",
                (cuid, name, now, now),
            )
            await self.core.atom_store.execute(
                "INSERT INTO user_identities (platform_id, uid, platform, display_name, first_seen, last_seen, source) VALUES (?,?,?,?,?,?,?)",
                (platform_id, cuid, "qq", name, now, now, "auto"),
            )
            return (cuid, name)

        except Exception as e:
            logger.warning(f"[memori] _ensure_user_identity 异常: {e}")
            display = event_sender_name or (f"用户{user_id[-4:]}" if len(user_id) >= 4 else "用户")
            return (user_id, display)

    # ── 记忆注入：LLM 请求前 ──

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.core:
            return

        raw_text = event.get_message_str() if hasattr(event, 'get_message_str') else str(event.message_str)
        if not raw_text or raw_text.startswith("/"):
            if hasattr(req, 'prompt'):
                req.prompt = None
            if req.contexts:
                req.contexts.clear()
            if hasattr(event, 'message_str'):
                event.message_str = ""
            return

        raw_uid = AstrBotCtx().get_user_id(event)
        cuid, display_name = await self._ensure_user_identity(raw_uid, self._get_sender_name(event))

        system_prompt = getattr(event, "system_prompt", "") or ""
        result = await self.core.process_message(
            user_id=cuid,
            message_text=raw_text,
            sender_name=display_name,
            system_prompt=system_prompt,
        )

        if result is not None:
            event.message_obj.message_str = result

        if hasattr(event, 'system_prompt') and event.system_prompt and req:
            req.system_prompt = event.system_prompt

    # ── 后台整理 ──

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        if not self.core:
            return
        try:
            raw_uid = AstrBotCtx().get_user_id(event)
            txt = AstrBotCtx().get_conversation_text(event)
            cuid, display_name = await self._ensure_user_identity(raw_uid, self._get_sender_name(event))

            if not cuid or not txt or txt.startswith("/"):
                return

            # 1. 写入 conversations.db（持久化）
            if self.core.conversation_store:
                try:
                    await self.core.conversation_store.add_message(
                        session_id=event.unified_msg_origin,
                        user_id=cuid,
                        role="user",
                        content=txt,
                        sender_name=display_name,
                    )
                except Exception:
                    pass

            # 3. 更新活动时间（供空闲超时使用）
            task = asyncio.ensure_future(
                self.core.consolidation_manager.on_message(cuid, txt, display_name)
            )
            self.core._background_tasks.add(task)
            task.add_done_callback(self.core._background_tasks.discard)
        except Exception as e:
            logger.error(f"[memori] on_message 出错: {e}")

    # ── LLM 响应记录 ──

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response: LLMResponse = None):
        if not self.core:
            return
        try:
            if response:
                raw_uid = AstrBotCtx().get_user_id(event)
                cuid, _ = await self._ensure_user_identity(raw_uid)
                resp_text = ""
                if hasattr(response, "result_chain") and response.result_chain:
                    resp_text = response.result_chain.get_plain_text() or ""
                if resp_text:
                    # 写入 conversations.db
                    if self.core.conversation_store:
                        try:
                            await self.core.conversation_store.add_message(
                                session_id=event.unified_msg_origin,
                                user_id=cuid,
                                role="assistant",
                                content=resp_text,
                            )
                        except Exception:
                            pass

                    # 累计一轮对话 → 可能触发整理
                    try:
                        await self.core.consolidation_manager.on_round_complete(
                            cuid, session_id=event.unified_msg_origin,
                        )
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"[memori] on_response 出错: {e}")

    # ── Dashboard 指令 ──

    @filter.command("memori_dashboard")
    async def dashboard(self, event: AstrMessageEvent):
        """获取记忆系统 Dashboard 访问地址"""
        if not self.core:
            yield event.plain_result("memori 内核未初始化")
            return
        port = self.config.get("api_port", 8765)
        yield event.plain_result(
            f" 记忆系统 Dashboard\n\n"
            f"面板地址: http://localhost:{port}/\n"
            f"设置页:   http://localhost:{port}/settings\n"
            f"API 文档: http://localhost:{port}/docs\n"
        )

    # ── 卸载清理 ──

    async def on_unload(self):
        # 停止 HTTP 服务 — 先发关闭信号，再等 task 结束，确保 socket 释放
        if hasattr(self, '_http_server_obj') and self._http_server_obj:
            self._http_server_obj.should_exit = True
        if hasattr(self, '_http_server') and self._http_server:
            try:
                await asyncio.wait_for(self._http_server, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._http_server.cancel()
                # 给操作系统一点时间回收 socket
                await asyncio.sleep(0.5)
        if self.core:
            await self.core.destroy()
            try:
                from .memori.storage.base_store import BaseDbStore
                BaseDbStore.close_all_sync()
            except Exception:
                pass
            logger.info("[memori] 内核已关闭")


# ═══════════════════════════════════════════════════════════════
#  HTTP 服务入口（独立运行时）
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    from memori.__main__ import main
    sys.exit(main())
