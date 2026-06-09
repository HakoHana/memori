"""AstrBot Memory Plugin — 日记式长期记忆插件"""

from __future__ import annotations

import asyncio

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api import logger

from .core.memory_core import MemoryCore
from .core.memory_tools import RecallMemoryTool, MemorizeMemoryTool


@register(
    name="Memory",
    author="your_name",
    desc="日记式长期记忆插件 — 让 Bot 记住与用户的每一刻",
    version="0.2.0",
    repo="https://github.com/your_name/astrbot_plugin_memory",
)
class MemoryPlugin(Star):
    """记忆插件主入口"""

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.memory_core: MemoryCore | None = None

    async def initialize(self):
        data_dir = str(StarTools.get_data_dir())
        logger.info(f"[Memory] 初始化: {data_dir}")
        self.memory_core = MemoryCore(
            plugin_context=self.context,
            data_dir=data_dir,
            config=self.config,
        )
        await self.memory_core.initialize()
        try:
            recall_tool = RecallMemoryTool()
            recall_tool.set_memory_core(self.memory_core)
            memorize_tool = MemorizeMemoryTool()
            memorize_tool.set_memory_core(self.memory_core)
            self.context.add_llm_tools(recall_tool, memorize_tool)
            # 强制激活工具（清除 inactivated_llm_tools 持久化记录）
            self.context.activate_llm_tool("recall_long_term_memory")
            self.context.activate_llm_tool("memorize_long_term_memory")
            logger.info("[Memory] Agent Tools 已注册")
            # 诊断：写入 func_list 状态 + star_map
            try:
                tmgr = self.context.get_llm_tool_manager()
                names = [t.name for t in tmgr.func_list]
                debug_p = data_dir + "/debug_init.txt"
                with open(debug_p, "w") as f:
                    f.write(f"func_list: {names}\n")
                    f.write(f"has recall: {'recall_long_term_memory' in names}\n")
                    # 检查工具的 handler_module_path
                    for t in tmgr.func_list:
                        if t.name == 'recall_long_term_memory' or t.name == 'memorize_long_term_memory':
                            f.write(f"  tool={t.name} mp={t.handler_module_path} active={t.active}\n")
                    # star_map keys
                    from astrbot.core.star.star import star_map
                    f.write(f"star_map keys: {list(star_map.keys())}\n")
                logger.info(f"[Memory] 诊断写入 {debug_p}")
            except Exception as e2:
                logger.warning(f"[Memory] 诊断写失败: {e2}")
        except Exception as e:
            logger.warning(f"[Memory] 注册 Agent Tools 失败: {e}")
        logger.info("[Memory] 初始化完成")

    def _get_sender_name(self, event) -> str:
        """从事件提取发送者显示名"""
        try:
            if hasattr(event, "get_sender_name"):
                name = event.get_sender_name()
                if name: return str(name)
            if hasattr(event, "sender_name"):
                name = event.sender_name
                if name: return str(name)
            if hasattr(event, "message_obj") and event.message_obj:
                sender = getattr(event.message_obj, "sender", None)
                if sender:
                    for attr in ("card", "nickname", "name", "user_displayname"):
                        val = getattr(sender, attr, None)
                        if val: return str(val)
        except Exception:
            pass
        return ""

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.memory_core:
            return
        logger.debug(f"[Memory] on_llm_request enter")
        from .core.context import current_user_id
        uid = self.memory_core.context_provider.get_user_id(event)
        current_user_id.set(uid)

        # 诊断：标记 on_llm_request 入口
        try:
            init_flag = self.memory_core._initialized if hasattr(self.memory_core, '_initialized') else 'NO_ATTR'
            with open('/tmp/recall_debug.txt', 'a') as _f:
                _f.write(f"[on_llm] uid={uid} initialized={init_flag}\n")
        except Exception:
            pass

        try:
            raw_text = event.get_message_str() if hasattr(event, 'get_message_str') else str(event.message_str)
            if raw_text.startswith("/"):
                if hasattr(req, 'prompt'):
                    req.prompt = None
                if req.contexts:
                    req.contexts.clear()
                event.message_str = ""
                if hasattr(event, 'message_obj') and event.message_obj:
                    event.message_obj.message_str = ""
                logger.debug(f"[Memory] on_llm_request: cmd={raw_text[:30]}, 跳过 LLM")
                return

            # 提取发送者信息
            uid = self.memory_core.context_provider.get_user_id(event)
            sender_name = self._get_sender_name(event)

            # 注册/更新用户名（双写：旧表 + 新身份体系）
            if self.memory_core.atom_store and uid:
                try:
                    await self.memory_core.atom_store.ensure_user(uid, sender_name)
                except Exception:
                    pass
                try:
                    await self.memory_core.atom_store.ensure_canonical_user(
                        f"qq:{uid}", sender_name, "qq"
                    )
                except Exception:
                    pass

            # 存储用户消息到会话
            cs = self.memory_core.conversation_store
            if cs and raw_text:
                sid = await cs.get_session_id(event)
                await cs.add_message(sid, uid, "user", raw_text, sender_name)

            # 记忆注入
            debug_p2 = str(self.memory_core.data_dir) + "/debug_req.txt"
            try:
                result = await self.memory_core.on_message(event)
                if result is not None:
                    event.message_obj.message_str = result
                    with open(debug_p2, "a") as f:
                        f.write(f"on_message 注入成功 ({len(result)} chars)\n")
                else:
                    with open(debug_p2, "a") as f:
                        f.write(f"on_message 返回 None（已注入到 system_prompt）\n")
                # 同步 event.system_prompt → req.system_prompt
                if hasattr(event, 'system_prompt') and event.system_prompt and req and event.system_prompt != req.system_prompt:
                    req.system_prompt = event.system_prompt
                    with open(debug_p2, "a") as f:
                        f.write(f"已同步 system_prompt 到 req\n")
            except Exception as e_inject:
                with open(debug_p2, "a") as f:
                    f.write(f"on_message 异常: {e_inject}\n")

            # 强制激活记忆工具（防止被 inactivated_llm_tools 反激活）
            try:
                tmgr2 = self.context.get_llm_tool_manager()
                for tool_name in ["recall_long_term_memory", "memorize_long_term_memory"]:
                    tool = tmgr2.get_func(tool_name)
                    if tool:
                        tool.active = True
                        if req and req.func_tool and tool_name not in req.func_tool.names():
                            req.func_tool.add_tool(tool)
                debug_p2 = str(self.memory_core.data_dir) + "/debug_req.txt"
                with open(debug_p2, "a") as f:
                    f.write(f"激活后 req.func_tool: {req.func_tool.names() if req and req.func_tool else 'N/A'}\n")
            except Exception as e_act:
                logger.warning(f"[Memory] 激活工具诊断异常: {e_act}")

            # 更新用户等级（轻量，每 ~10 条消息才重算）
            try:
                await self.memory_core._maybe_update_tier(uid)
            except Exception:
                pass
        except Exception as e:
            logger.error(f"[Memory] on_llm_request 出错: {e}")
            import traceback
            traceback.print_exc()
        finally:
            logger.debug(f"[Memory] on_llm_request exit (raw={raw_text[:40] if raw_text else 'none'})")

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        if not self.memory_core:
            return
        try:
            uid = self.memory_core.context_provider.get_user_id(event)
            txt = self.memory_core.context_provider.get_conversation_text(event)

            # 预过滤：新用户/噪声消息直接丢弃（不经过 LLM）
            # 可在插件配置界面关闭（pre_filter_enabled = false）
            if txt and not txt.startswith("/") and uid and self.config.get("pre_filter_enabled", False):
                try:
                    if await self.memory_core.should_ignore(uid, txt):
                        event.message_str = ""
                        if hasattr(event, 'message_obj') and event.message_obj:
                            event.message_obj.message_str = ""
                        return
                except Exception:
                    pass

            # 检测指令 → 在 LLM 处理前拦截，直接回复
            if txt and txt.startswith("/"):
                event.message_str = ""
                if hasattr(event, 'message_obj') and event.message_obj:
                    event.message_obj.message_str = ""

                if txt.strip().startswith("/记忆重构"):
                    from astrbot.core.message.message_event_result import MessageChain
                    parts = txt.strip().split(maxsplit=1)
                    args = parts[1:] if len(parts) > 1 else []
                    chain = MessageChain().message("🔄 正在逐条重构旧记忆，请稍候...")
                    await event.send(chain)

                    result = await self.memory_core.command_handler.handle_rebuild(uid, args)
                    chain2 = MessageChain().message(result)
                    await event.send(chain2)
                else:
                    await self.memory_core._handle_command(uid, txt)
                return

            if uid and txt:
                logger.debug(f"[Memory] on_message: {uid}")
                task = asyncio.ensure_future(
                    self.memory_core.consolidation_manager.on_message(uid, txt)
                )
                self.memory_core._background_tasks.add(task)
                task.add_done_callback(self.memory_core._background_tasks.discard)
        except Exception as e:
            logger.error(f"[Memory] on_message 出错: {e}")

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response: LLMResponse = None):
        if not self.memory_core:
            return
        try:
            # 存储 Bot 回复
            cs = self.memory_core.conversation_store
            if cs and response:
                sid = await cs.get_session_id(event)
                uid = self.memory_core.context_provider.get_user_id(event)
                resp_text = ""
                if hasattr(response, "result_chain") and response.result_chain:
                    resp_text = response.result_chain.get_plain_text() or ""
                if resp_text:
                    bot_name = getattr(event, "bot_name", "") or "Hana"
                    await cs.add_message(sid, uid, "assistant", resp_text, bot_name)

        except Exception as e:
            logger.error(f"[Memory] on_response 出错: {e}")

    async def on_unload(self):
        if self.memory_core:
            await self.memory_core.destroy()
            try:
                from .storage.base_store import BaseDbStore
                BaseDbStore.close_all_sync()
            except Exception:
                pass
            logger.info("[Memory] 已卸载")

    # 解释器退出时的最后兜底（同步关闭所有 aiosqlite 连接）
    @staticmethod
    def _atexit_cleanup():
        try:
            from .storage.base_store import BaseDbStore
            BaseDbStore.close_all_sync()
        except Exception:
            pass
