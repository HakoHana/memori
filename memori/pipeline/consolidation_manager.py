"""调度器 + 会话状态管理器 — 按 bot 参与轮数触发，重量操作委托 WarmProcessor

触发器设计：
A. 对话轮数 — bot 回复后累计一轮，达到阈值触发整理（主触发）
B. 空闲超时 — 用户超过 N 分钟无活动 → 扫描未整理内容兜底整理（安全网）
C. 定时扫描 — 周期检查积压，防止疏漏（安全网）

全局限速防止多个用户同时触发挤爆 LLM 队列。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from ..core.logger import logger

from ..models.memory_atom import PersistedSessionState
from ..storage.state_store import StateStore
from ..core.interfaces import IConsolidationManager, IWarmProcessor


class ConsolidationManager(IConsolidationManager):
    """
    调度器 + 会话状态管理器

    职责（轻量）：
    - 对话轮数计数（只在 bot 回复后累加）
    - 空闲超时兜底
    - 全局限速 + 用户级限速
    - 会话状态持久化（延迟刷写）

    重量操作（LLM 调用、DB 写入）委托给 WarmProcessor 异步队列。
    """

    def __init__(
        self,
        state_store: StateStore,
        warm_processor=None,
        conversation_store=None,
        config: dict[str, Any] | None = None,
    ):
        self.state_store = state_store
        self.warm_processor = warm_processor
        self.conversation_store = conversation_store
        self.config = config or {}

        # 配置
        self._round_threshold = self.config.get("consolidation_rounds", 10)  # 每 N 轮 bot 对话触发一次
        self.idle_timeout_minutes = self.config.get("idle_timeout_minutes", 60)
        self._scan_interval_minutes = self.config.get("scan_interval_minutes", 120)
        self._min_global_interval = self.config.get("min_global_interval", 120)
        self._min_user_interval = self.config.get("min_user_interval", 60)

        # 会话状态（内存中，延迟写回）
        self._states: dict[str, PersistedSessionState] = {}
        self._dirty_users: set[str] = set()
        self._flush_task: asyncio.Task | None = None
        self._flush_interval: float = 5.0

        # 空闲检测
        self._idle_check_task: asyncio.Task | None = None
        self._idle_check_interval: float = 60.0
        self._last_activity: dict[str, float] = {}

        # 定时扫描
        self._periodic_scan_task: asyncio.Task | None = None

        # 去抖（限速间隔内不重复触发）
        self._debounce_interval: float = 10.0
        self._last_trigger_check: dict[str, float] = {}
        self._pending_counts: dict[str, int] = {}

        # 全局限速
        self._global_last_consolidation: float = 0.0

        # 用户级限速
        self._last_user_consolidation: dict[str, float] = {}

        # 每个用户最新的 session_id（供空闲/扫描查 DB）
        self._last_session_id: dict[str, str] = {}

        self._destroyed = False

    async def initialize(self):
        """从数据库恢复所有会话状态"""
        states = await self.state_store.load_all()
        self._states = states
        now = time.time()
        for uid in states:
            if states[uid].msg_count > 0:
                self._last_activity[uid] = now

        self._flush_task = asyncio.create_task(self._flush_loop())
        self._idle_check_task = asyncio.create_task(self._idle_check_loop())
        self._periodic_scan_task = asyncio.create_task(self._periodic_scan_loop())

    async def destroy(self):
        """销毁调度器"""
        self._destroyed = True
        pending_tasks = []
        for t in (self._flush_task, self._idle_check_task, self._periodic_scan_task):
            if t and not t.done():
                t.cancel()
                pending_tasks.append(t)
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)

        if self._dirty_users:
            await self._flush_dirty_states()
        for uid, state in self._states.items():
            await self.state_store.save(state)
        self._states.clear()
        self._dirty_users.clear()

    # ═══════════════════════════════════════════════════
    #  A. 主触发：on_round_complete（bot 回复后调用）
    # ═══════════════════════════════════════════════════

    async def on_round_complete(self, user_id: str, session_id: str = ""):
        """Bot 完成一轮对话后调用，累计轮数并在达到阈值时触发整理

        Args:
            user_id: 用户 ID
            session_id: 会话 ID，用于从 conversations.db 拉上下文
        """
        if self._destroyed:
            return

        state = self._get_or_create_state(user_id)
        state.msg_count += 1  # 每轮 +1
        self._last_activity[user_id] = time.time()
        if session_id:
            self._last_session_id[user_id] = session_id
        self._mark_dirty(user_id)

        # 检查是否达到触发阈值
        if state.msg_count < self._round_threshold:
            return

        # 用户级限速
        now = time.time()
        last_user = self._last_user_consolidation.get(user_id, 0.0)
        if now - last_user < self._min_user_interval:
            logger.debug(f"[Memory] 用户级限速: {user_id} 距上次 {now - last_user:.0f}s")
            return

        # 全局限速
        if self._global_last_consolidation > 0 and now - self._global_last_consolidation < self._min_global_interval:
            logger.debug(f"[Memory] 全局限速: 距上次 {now - self._global_last_consolidation:.0f}s")
            return

        conv_text = await self._get_conversation_context(user_id)
        logger.info(f"[Memory] 对话轮数触发整理: uid={user_id}, rounds={state.msg_count}")
        if self.warm_processor:
            await self.warm_processor.enqueue(user_id, conv_text, state, on_done=self._after_consolidation)

    # ═══════════════════════════════════════════════════
    #  on_message（AstrBot 每条消息调一次，仅用于更新最后活动时间）
    # ═══════════════════════════════════════════════════

    async def on_message(self, user_id: str, conversation_text: str, sender_name: str = ""):
        """每次有消息到来时的入口（仅更新活动时间，不计数）

        Args:
            user_id: 用户 ID
            conversation_text: 消息内容（仅记录活动用）
            sender_name: 发送者昵称
        """
        if self._destroyed:
            return
        self._last_activity[user_id] = time.time()

    # ═══════════════════════════════════════════════════
    #  整理完成回调
    # ═══════════════════════════════════════════════════

    async def _after_consolidation(self, user_id: str, result):
        """整理完成后的收尾"""
        now = time.time()
        self._global_last_consolidation = now
        self._last_user_consolidation[user_id] = now

        state = self._get_or_create_state(user_id)
        state.reset_after_consolidation()

        # 滑窗：记录最新消息 ID，下次只查增量
        if result and result.wrote_diary:
            sid = self._last_session_id.get(user_id, "")
            if sid and self.conversation_store:
                try:
                    row = await self.conversation_store.fetchone(
                        "SELECT MAX(id) FROM messages WHERE session_id=?", (sid,)
                    )
                    if row and row[0]:
                        state.last_consolidated_msg_id = row[0]
                except Exception:
                    pass

        self._mark_dirty(user_id)

    # ═══════════════════════════════════════════════════
    #  延迟刷写
    # ═══════════════════════════════════════════════════

    def _mark_dirty(self, user_id: str):
        self._dirty_users.add(user_id)

    async def _flush_loop(self):
        while not self._destroyed:
            try:
                await asyncio.sleep(self._flush_interval)
                if self._dirty_users:
                    await self._flush_dirty_states()
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    async def _flush_dirty_states(self):
        dirty = list(self._dirty_users)
        self._dirty_users.clear()
        for uid in dirty:
            state = self._states.get(uid)
            if state:
                try:
                    await self.state_store.save(state)
                except Exception as e:
                    logger.warning(f"[Memory] 状态刷写失败 {uid}: {e}")
                    self._dirty_users.add(uid)

    # ═══════════════════════════════════════════════════
    #  B. 空闲超时兜底
    # ═══════════════════════════════════════════════════

    async def _idle_check_loop(self):
        timeout_sec = self.idle_timeout_minutes * 60
        while not self._destroyed:
            try:
                await asyncio.sleep(self._idle_check_interval)
                now = time.time()
                for uid, last_active in list(self._last_activity.items()):
                    if self._destroyed:
                        return
                    if now - last_active < timeout_sec:
                        continue

                    state = self._states.get(uid)
                    if not state:
                        continue

                    if state.msg_count <= 0:
                        continue

                    # 用户级限速
                    last_user = self._last_user_consolidation.get(uid, 0.0)
                    if now - last_user < self._min_user_interval:
                        continue

                    # 全局限速
                    if self._global_last_consolidation > 0 and now - self._global_last_consolidation < self._min_global_interval:
                        continue

                    logger.info(f"[Memory] 空闲超时触发: {uid}")
                    conv_text = await self._get_conversation_context(uid)
                    if self.warm_processor:
                        await self.warm_processor.enqueue(uid, conv_text, state, on_done=self._after_consolidation)
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    # ═══════════════════════════════════════════════════
    #  C. 定时扫描（安全网）
    # ═══════════════════════════════════════════════════

    async def _periodic_scan_loop(self):
        """定时扫描：每 scan_interval_minutes 扫一遍所有用户"""
        interval_sec = self._scan_interval_minutes * 60
        while not self._destroyed:
            try:
                await asyncio.sleep(interval_sec)
                if self._destroyed:
                    return
                now = time.time()
                for uid, state in list(self._states.items()):
                    if self._destroyed:
                        return
                    if now - state.last_consolidated_at < interval_sec:
                        continue
                    if state.msg_count <= 0:
                        continue
                    # 用户级限速
                    last_user = self._last_user_consolidation.get(uid, 0.0)
                    if now - last_user < self._min_user_interval:
                        continue
                    # 全局限速
                    if self._global_last_consolidation > 0 and now - self._global_last_consolidation < self._min_global_interval:
                        continue
                    logger.info(f"[Memory] 定时扫描触发: uid={uid}")
                    conv_text = await self._get_conversation_context(uid)
                    if self.warm_processor:
                        await self.warm_processor.enqueue(uid, conv_text, state, on_done=self._after_consolidation)
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    # ═══════════════════════════════════════════════════
    #  状态管理
    # ═══════════════════════════════════════════════════

    def _get_or_create_state(self, user_id: str) -> PersistedSessionState:
        if user_id not in self._states:
            self._states[user_id] = PersistedSessionState(user_id=user_id)
        return self._states[user_id]

    def get_state(self, user_id: str) -> PersistedSessionState | None:
        return self._states.get(user_id)

    def set_warm_processor(self, warm_processor: IWarmProcessor):
        self.warm_processor = warm_processor

    # ── 辅助 ──

    async def _get_conversation_context(self, user_id: str) -> str:
        """获取用户自上次整理后的新对话上下文（滑窗）"""
        sid = self._last_session_id.get(user_id, "")
        if sid and self.conversation_store:
            try:
                state = self._states.get(user_id)
                after_id = state.last_consolidated_msg_id if state else 0
                text = await self.conversation_store.get_context_since(
                    sid, after_id=after_id, limit=50,
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
        self._min_user_interval = config.get("min_user_interval", self._min_user_interval)
