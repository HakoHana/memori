"""AstrBot Star 插件入口 — 将 memori 记忆内核接入 AstrBot

插件功能:
1. 消息拦截：@on_llm_request 注入记忆上下文
2. 后台整理：@on_message 触发记忆提取
3. LLM 响应记录：@on_llm_response 保存对话历史
4. Agent Tools：注册 RecallTool / MemorizeTool
5. Dashboard 指令：/memori_dashboard 获取访问地址
"""

from __future__ import annotations

import asyncio

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api import logger
from astrbot.core.star.filter.command import CommandFilter

from memori import MemoryCore
from .adapter import AstrBotLLM, AstrBotCtx
from .tools import RecallTool, MemorizeTool


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

        # 注册 Agent Tools
        try:
            recall_tool = RecallTool()
            recall_tool.set_core(self.core)
            memorize_tool = MemorizeTool()
            memorize_tool.set_core(self.core)
            self.context.add_llm_tools(recall_tool, memorize_tool)
            self.context.activate_llm_tool("recall_long_term_memory")
            self.context.activate_llm_tool("memorize_long_term_memory")
            logger.info("[memori] Agent Tools 已注册")
        except Exception as e:
            logger.warning(f"[memori] 注册 Agent Tools 失败: {e}")

        logger.info("[memori] 内核就绪")

    def _get_sender_name(self, event) -> str:
        try:
            return AstrBotCtx().get_sender_name(event)
        except Exception:
            return ""

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

        uid = AstrBotCtx().get_user_id(event)
        sender_name = self._get_sender_name(event)

        # 注册用户身份
        if self.core.atom_store and uid:
            try:
                await self.core.atom_store.ensure_user(uid, sender_name)
            except Exception:
                pass
            try:
                await self.core.atom_store.ensure_canonical_user(f"qq:{uid}", sender_name, "qq")
            except Exception:
                pass

        # 存储到会话
        cs = self.core.conversation_store
        if cs and raw_text:
            sid = await cs.get_session_id(event)
            await cs.add_message(sid, uid, "user", raw_text, sender_name)

        # 记忆注入
        system_prompt = getattr(event, "system_prompt", "") or ""
        result = await self.core.process_message(
            user_id=uid,
            message_text=raw_text,
            sender_name=sender_name,
            system_prompt=system_prompt,
        )

        if result is not None:
            event.message_obj.message_str = result

        if hasattr(event, 'system_prompt') and event.system_prompt and req:
            req.system_prompt = event.system_prompt

    # ── 后台整理：消息触发 ──

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """后台整理触发器（不阻塞消息回复）"""
        if not self.core:
            return
        try:
            uid = AstrBotCtx().get_user_id(event)
            txt = AstrBotCtx().get_conversation_text(event)
            sender_name = self._get_sender_name(event)

            if not uid or not txt or txt.startswith("/"):
                return

            cs = self.core.conversation_store
            full_ctx = txt
            if cs:
                try:
                    sid = await cs.get_session_id(event)
                    bot_name = self.config.get("bot_name", "Hana")
                    full_ctx = await cs.get_recent_context(sid, limit=10, bot_name=bot_name)
                except Exception:
                    pass

            task = asyncio.ensure_future(
                self.core.consolidation_manager.on_message(uid, full_ctx, sender_name)
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
            cs = self.core.conversation_store
            if cs and response:
                sid = await cs.get_session_id(event)
                uid = AstrBotCtx().get_user_id(event)
                resp_text = ""
                if hasattr(response, "result_chain") and response.result_chain:
                    resp_text = response.result_chain.get_plain_text() or ""
                if resp_text:
                    bot_name = self.config.get("bot_name", "Hana")
                    await cs.add_message(sid, uid, "assistant", resp_text, bot_name)
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
            f"配置页:   http://localhost:{port}/webui/settings/\n\n"
            f"确保 memori HTTP 服务正在运行（python -m memori --port {port}）"
        )

    # ── 卸载清理 ──

    async def on_unload(self):
        if self.core:
            await self.core.destroy()
            try:
                from memori.storage.base_store import BaseDbStore
                BaseDbStore.close_all_sync()
            except Exception:
                pass
            logger.info("[memori] 内核已关闭")
