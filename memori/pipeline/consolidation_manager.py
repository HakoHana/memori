"""调度器 + 会话状态管理器 — 按 session 累计轮数，DB 消息数决定触发

触发器设计（参照 livingmemory）：
A. 对话轮数 — bot 回复后查 DB 未处理消息数，达到阈值触发整理（主触发）
B. 空闲超时 — session 超过 N 分钟无活动 → 兜底整理（安全网）
C. 定时扫描 — 周期检查积压，防止疏漏（安全网）

滑窗位置（last_consolidated_msg_id）直接存到 conversations.db 的
sessions.metadata JSON 字段中，不维护独立的 consolidation_state 表。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from ..core.logger import logger

from ..models.memory_atom import PersistedSessionState
from ..core.interfaces import IConsolidationManager, IWarmProcessor


class ConsolidationManager(IConsolidationManager):
    """
    调度器 + 会话状态管理器

    职责（轻量）：
    - 按 session 累计对话轮数（从 DB 查未处理消息数决定触发）
    - 空闲超时兜底（session 级别）
    - 会话状态持久化（存入 sessions.metadata）

    重量操作（LLM 调用、DB 写入）委托给 WarmProcessor 异步队列。
    """

    def __init__(
        self,
        conversation_store=None,
        warm_processor=None,
        config: dict[str, Any] | None = None,
    ):
        self.conversation_store = conversation_store
        self.warm_processor = warm_processor
        self.config = config or {}

        # 配置
        self._round_threshold = self.config.get("consolidation_rounds", 10)
        self.idle_timeout_minutes = self.config.get("idle_timeout_minutes", 60)
        self._scan_interval_minutes = self.config.get("scan_interval_minutes", 120)
        self._min_global_interval = self.config.get("min_global_interval", 120)

        # ── 会话状态 key=session_id（一个 session 共享一个计数器） ──
        self._states: dict[str, PersistedSessionState] = {}

        # ── 空闲检测（key=session_id） ──
        self._idle_check_task: asyncio.Task | None = None
        self._idle_check_interval: float = 60.0
        self._last_activity: dict[str, float] = {}

        # ── 定时扫描 ──
        self._periodic_scan_task: asyncio.Task | None = None

        # ── 全局限速 ──
        self._global_last_consolidation: float = 0.0

        # ── 正在整理中的 session（防重复触发） ──
        self._inflight_sessions: set[str] = set()

        self._destroyed = False

    async def initialize(self):
        """从 conversations.db 的 sessions 表恢复会话状态"""
        if self.conversation_store:
            try:
                rows = await self.conversation_store.fetch(
                    "SELECT session_id, metadata FROM sessions"
                )
                now = time.time()
                for session_id, meta_json in rows:
                    meta = json.loads(meta_json) if isinstance(meta_json, str) else (meta_json or {})
                    state = PersistedSessionState(user_id=session_id)
                    state.last_consolidated_msg_id = meta.get("last_consolidated_msg_id", 0)
                    state.last_consolidated_at = meta.get("last_consolidated_at", 0.0)
                    state.diary_count = meta.get("diary_count", 0)
                    state.diary_count_since_persona = meta.get("diary_count_since_persona", 0)
                    state.last_diary_date = meta.get("last_diary_date", "")
                    if state.last_consolidated_at > 0:
                        self._last_activity[session_id] = max(
                            self._last_activity.get(session_id, 0), state.last_consolidated_at
                        )
                    self._states[session_id] = state
                logger.info(f"[Memory] 已从 sessions 表恢复 {len(rows)} 个会话状态")
            except Exception as e:
                logger.warning(f"[Memory] 加载会话状态失败（首次运行可忽略）: {e}")

        self._idle_check_task = asyncio.create_task(self._idle_check_loop())
        if self.config.get("periodic_scan_enabled", True):
            self._periodic_scan_task = asyncio.create_task(self._periodic_scan_loop())
        else:
            logger.info("[Memory] 定时扫描已关闭（periodic_scan_enabled=false）")

    async def destroy(self):
        """销毁调度器"""
        self._destroyed = True
        pending_tasks = []
        for t in (self._idle_check_task, self._periodic_scan_task):
            if t and not t.done():
                t.cancel()
                pending_tasks.append(t)
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)

        # 写回所有脏状态
        for session_id, state in self._states.items():
            await self._save_state_to_metadata(session_id, state)
        self._states.clear()

    # ═══════════════════════════════════════════════════
    #  状态持久化（直接读写 sessions.metadata）
    # ═══════════════════════════════════════════════════

    async def _save_state_to_metadata(self, session_id: str, state: PersistedSessionState):
        """将状态写入 sessions 表的 metadata JSON 字段"""
        if not self.conversation_store:
            return
        try:
            # 读取当前 metadata（保留其他插件可能写入的字段）
            row = await self.conversation_store.fetchone(
                "SELECT metadata FROM sessions WHERE session_id=?", (session_id,)
            )
            meta = {}
            if row and row[0]:
                meta = json.loads(row[0]) if isinstance(row[0], str) else row[0]

            # 覆写记忆整理相关字段
            meta["last_consolidated_msg_id"] = state.last_consolidated_msg_id
            meta["last_consolidated_at"] = state.last_consolidated_at
            meta["diary_count"] = state.diary_count
            meta["diary_count_since_persona"] = state.diary_count_since_persona
            meta["last_diary_date"] = state.last_diary_date

            meta_json = json.dumps(meta, ensure_ascii=False)

            # 确保 session 行存在（add_message 创建前也可能被保存）
            await self.conversation_store.execute(
                "INSERT OR IGNORE INTO sessions(session_id, user_id, created_at, last_active_at) "
                "VALUES (?, ?, ?, ?)",
                (session_id, session_id, time.time(), time.time()),
            )
            await self.conversation_store.execute(
                "UPDATE sessions SET metadata = ? WHERE session_id = ?",
                (meta_json, session_id),
            )
        except Exception as e:
            logger.warning(f"[Memory] 保存 session 元数据失败: {e}")

    # ═══════════════════════════════════════════════════
    #  A. 主触发：on_round_complete（bot 回复后调用）
    # ═══════════════════════════════════════════════════

    async def on_round_complete(self, user_id: str, session_id: str = ""):
        """Bot 完成一轮对话后调用，查 DB 未处理消息数 → 达到阈值触发整理

        计数方式：查 conversations.db 中该 session 的总消息数，
        减掉 last_consolidated_msg_id（上次整理时最新的消息 ID），
        剩余未处理消息 ÷ 2 = 未处理对话轮数（每轮 = 1 user msg + 1 bot msg）。

        Args:
            user_id: 触发此轮对话的用户 ID（仅用于日志跟踪）
            session_id: 会话 ID（查 DB 和找 state 的 key）
        """
        if self._destroyed or not session_id:
            return

        state = self._get_or_create_state(session_id)
        self._last_activity[session_id] = time.time()

        # 从 DB 查未处理轮数
        unsummarized_rounds = await self._count_unprocessed_rounds(session_id, state)
        if unsummarized_rounds < self._round_threshold:
            return

        # 全局限速
        now = time.time()
        if self._global_last_consolidation > 0 and now - self._global_last_consolidation < self._min_global_interval:
            logger.debug(f"[Memory] 全局限速: 距上次 {now - self._global_last_consolidation:.0f}s")
            return

        conv_text = await self._get_conversation_context(session_id)
        if not conv_text:
            logger.debug(f"[Memory] 无新对话内容可整理: session={session_id}")
            return

        logger.info(
            f"[Memory] 对话轮数触发整理: session={session_id}, "
            f"unprocessed_rounds={unsummarized_rounds}/{self._round_threshold}"
        )

        # 检查是否已有同一 session 的整理任务在排队/执行中
        if session_id in self._inflight_sessions:
            logger.debug(f"[Memory] {session_id} 正在整理中，跳过轮数触发")
            return

        async def _done_cb(_uid, result):
            await self._after_consolidation(session_id, result)

        if self.warm_processor:
            self._inflight_sessions.add(session_id)
            try:
                await self.warm_processor.enqueue(
                    user_id, conv_text, state, on_done=_done_cb,
                )
            except Exception as e:
                self._inflight_sessions.discard(session_id)
                logger.warning(f"[Memory] 入队失败，已释放 inflight 锁: {e}")

    # ═══════════════════════════════════════════════════
    #  on_message（每条消息调一次，仅更新活动时间）
    # ═══════════════════════════════════════════════════

    async def on_message(self, user_id: str, conversation_text: str, sender_name: str = "", session_id: str = ""):
        """每次有消息到来时的入口（仅更新 session 活动时间，不计数）

        Args:
            user_id: 用户 ID
            conversation_text: 消息内容（仅记录活动用）
            sender_name: 发送者昵称
            session_id: 会话 ID
        """
        if self._destroyed:
            return
        if session_id:
            self._last_activity[session_id] = time.time()

    # ═══════════════════════════════════════════════════
    #  整理完成回调
    # ═══════════════════════════════════════════════════

    async def _after_consolidation(self, session_id: str, result):
        """整理完成后的收尾

        Args:
            session_id: 已整理的会话 ID
            result: CaptureResult
        """
        now = time.time()
        self._global_last_consolidation = now

        state = self._get_or_create_state(session_id)
        state.reset_after_consolidation()

        # 无论是否写了日记，只要被处理了就推进滑窗
        # 防止「不值得记」的判断导致同一批消息无限重复整理
        if self.conversation_store:
            try:
                row = await self.conversation_store.fetchone(
                    "SELECT MAX(id) FROM messages WHERE session_id=?", (session_id,)
                )
                if row and row[0]:
                    state.last_consolidated_msg_id = row[0]
                    logger.debug(
                        f"[Memory] 已更新滑窗位置: session={session_id}, "
                        f"last_consolidated_msg_id={state.last_consolidated_msg_id}"
                    )
            except Exception as e:
                logger.warning(f"[Memory] 更新滑窗位置失败: {e}")

        # 先落盘，再放锁 — 防止重载时 state 丢失导致滑窗回退
        await self._save_state_to_metadata(session_id, state)

        # 释放 inflight 锁，允许该 session 再次触发整理
        self._inflight_sessions.discard(session_id)

    # ═══════════════════════════════════════════════════
    #  B. 空闲超时兜底（session 级别）
    # ═══════════════════════════════════════════════════

    async def _idle_check_loop(self):
        timeout_sec = self.idle_timeout_minutes * 60
        while not self._destroyed:
            try:
                await asyncio.sleep(self._idle_check_interval)
                now = time.time()
                for session_id, last_active in list(self._last_activity.items()):
                    if self._destroyed:
                        return
                    if now - last_active < timeout_sec:
                        continue

                    state = self._states.get(session_id)
                    if not state:
                        continue

                    unprocessed = await self._count_unprocessed_messages(session_id, state)
                    if unprocessed < 2:  # 至少一轮对话
                        continue

                    if self._global_last_consolidation > 0 and now - self._global_last_consolidation < self._min_global_interval:
                        continue

                    logger.info(f"[Memory] 空闲超时触发: session={session_id}, idle={(now - last_active)/60:.0f}min")

                    # 检查是否已有同一 session 的整理任务在排队/执行中
                    if session_id in self._inflight_sessions:
                        logger.debug(f"[Memory] {session_id} 正在整理中，跳过空闲触发")
                        continue

                    conv_text = await self._get_conversation_context(session_id)
                    if not conv_text:
                        continue

                    # await 期间可能已有其他 trigger 入队，重新检查互斥锁
                    if session_id in self._inflight_sessions:
                        logger.debug(f"[Memory] {session_id} 空闲触发: await 后检查到 inflight，跳过")
                        continue

                    async def _done_cb(_uid, result, _sid=session_id):
                        await self._after_consolidation(_sid, result)

                    if self.warm_processor:
                        self._inflight_sessions.add(session_id)
                        try:
                            await self.warm_processor.enqueue(
                                session_id, conv_text, state, on_done=_done_cb,
                            )
                        except Exception as e:
                            self._inflight_sessions.discard(session_id)
                            logger.warning(f"[Memory] 空闲触发入队失败，已释放 inflight 锁: {e}")
                            continue
                    # 更新活动时间，防止下次空闲检测再次入队
                    self._last_activity[session_id] = time.time()
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    # ═══════════════════════════════════════════════════
    #  C. 定时扫描（安全网）
    # ═══════════════════════════════════════════════════

    async def _periodic_scan_loop(self):
        """定时扫描：每 scan_interval_minutes 扫一遍所有 session"""
        interval_sec = self._scan_interval_minutes * 60
        while not self._destroyed:
            try:
                await asyncio.sleep(interval_sec)
                if self._destroyed:
                    return
                now = time.time()
                for session_id, state in list(self._states.items()):
                    if self._destroyed:
                        return
                    if now - state.last_consolidated_at < interval_sec:
                        continue

                    unprocessed = await self._count_unprocessed_messages(session_id, state)
                    if unprocessed < 2:
                        continue

                    if self._global_last_consolidation > 0 and now - self._global_last_consolidation < self._min_global_interval:
                        continue

                    logger.info(f"[Memory] 定时扫描触发: session={session_id}")

                    # 检查是否已有同一 session 的整理任务在排队/执行中
                    if session_id in self._inflight_sessions:
                        logger.debug(f"[Memory] {session_id} 正在整理中，跳过定时扫描")
                        continue

                    conv_text = await self._get_conversation_context(session_id)
                    if not conv_text:
                        continue

                    # await 期间可能已有其他 trigger 入队，重新检查互斥锁
                    if session_id in self._inflight_sessions:
                        logger.debug(f"[Memory] {session_id} 定时扫描: await 后检查到 inflight，跳过")
                        continue

                    async def _done_cb(_uid, result, _sid=session_id):
                        await self._after_consolidation(_sid, result)

                    if self.warm_processor:
                        self._inflight_sessions.add(session_id)
                        try:
                            await self.warm_processor.enqueue(
                                session_id, conv_text, state, on_done=_done_cb,
                            )
                        except Exception as e:
                            self._inflight_sessions.discard(session_id)
                            logger.warning(f"[Memory] 定时扫描入队失败，已释放 inflight 锁: {e}")
                            continue
                    # 更新活动时间，防止空闲检测再次触发同一 session
                    self._last_activity[session_id] = time.time()
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    # ═══════════════════════════════════════════════════
    #  DB 查询辅助
    # ═══════════════════════════════════════════════════

    async def _count_unprocessed_messages(self, session_id: str, state: PersistedSessionState | None) -> int:
        """查 DB 获取 session 中未处理的消息数（滑窗：只看 id > 上次整理位置的消息）"""
        if not self.conversation_store:
            return 0
        try:
            after_id = state.last_consolidated_msg_id if state else 0
            row = await self.conversation_store.fetchone(
                "SELECT COUNT(*) FROM messages WHERE session_id=? AND id>?", (session_id, after_id)
            )
            return row[0] if row else 0
        except Exception:
            return 0

    async def _count_unprocessed_rounds(self, session_id: str, state: PersistedSessionState | None) -> int:
        """查 DB 获取 session 中未处理的对话轮数（2 条消息 = 1 轮）"""
        return (await self._count_unprocessed_messages(session_id, state)) // 2

    # ═══════════════════════════════════════════════════
    #  状态管理
    # ═══════════════════════════════════════════════════

    def _get_or_create_state(self, session_id: str) -> PersistedSessionState:
        if session_id not in self._states:
            self._states[session_id] = PersistedSessionState(user_id=session_id)
        return self._states[session_id]

    def get_state(self, session_id: str) -> PersistedSessionState | None:
        return self._states.get(session_id)

    def set_warm_processor(self, warm_processor: IWarmProcessor):
        self.warm_processor = warm_processor

    # ── 辅助 ──

    async def _get_conversation_context(self, session_id: str) -> str:
        """获取 session 自上次整理后的新对话上下文（滑窗）"""
        if session_id and self.conversation_store:
            try:
                state = self._states.get(session_id)
                after_id = state.last_consolidated_msg_id if state else 0
                text = await self.conversation_store.get_context_since(
                    session_id, after_id=after_id, limit=50,
                )
                if text:
                    return text
            except Exception:
                pass
        return ""

    def update_config(self, config: dict[str, Any]):
        """热更新配置"""
        if "consolidation_rounds" in config:
            self._round_threshold = int(config["consolidation_rounds"])
        self.idle_timeout_minutes = config.get("idle_timeout_minutes", self.idle_timeout_minutes)
        self._scan_interval_minutes = config.get("scan_interval_minutes", self._scan_interval_minutes)
        self._min_global_interval = config.get("min_global_interval", self._min_global_interval)
        if "periodic_scan_enabled" in config:
            enabled = config["periodic_scan_enabled"]
            if enabled and (self._periodic_scan_task is None or self._periodic_scan_task.done()):
                self._periodic_scan_task = asyncio.create_task(self._periodic_scan_loop())
                logger.info("[Memory] 定时扫描已开启（periodic_scan_enabled=true）")
            elif not enabled and self._periodic_scan_task and not self._periodic_scan_task.done():
                self._periodic_scan_task.cancel()
                self._periodic_scan_task = None
                logger.info("[Memory] 定时扫描已关闭（periodic_scan_enabled=false）")
