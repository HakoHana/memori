"""调度器 + 会话状态管理器 — 仅保留条件计数，重量操作委托 WarmProcessor"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ..core.logger import logger

from ..models.memory_atom import PersistedSessionState
from ..storage.state_store import StateStore
from ..core.interfaces import IConsolidationManager, IWarmProcessor


class ConsolidationManager(IConsolidationManager):
    """
    调度器 + 会话状态管理器

    职责（轻量）：
    - 消息计数 & 去抖
    - 触发条件判断（条数阈值 / 时间间隔 / 暖启动）
    - 全局限速
    - 会话状态持久化（延迟刷写）
    - 空闲超时兜底

    重量操作（LLM 调用、DB 写入）委托给 WarmProcessor 异步队列。
    """

    def __init__(
        self,
        state_store: StateStore,
        warm_processor=None,
        config: dict[str, Any] | None = None,
    ):
        self.state_store = state_store
        self.warm_processor = warm_processor
        self.config = config or {}

        # 配置
        self.trigger_msg_count = self.config.get("trigger_msg_count", 10)
        self.trigger_time_minutes = self.config.get("trigger_time_minutes", 360)
        self.warmup_enabled = self.config.get("warmup_enabled", True)
        self.idle_timeout_minutes = self.config.get("idle_timeout_minutes", 30)

        # 会话状态（内存中，延迟写回）
        self._states: dict[str, PersistedSessionState] = {}
        self._dirty_users: set[str] = set()
        self._flush_task: asyncio.Task | None = None
        self._flush_interval: float = 5.0

        # 空闲检测
        self._idle_check_task: asyncio.Task | None = None
        self._idle_check_interval: float = 60.0
        self._last_activity: dict[str, float] = {}

        # 去抖
        self._debounce_interval: float = 10.0
        self._last_trigger_check: dict[str, float] = {}
        self._pending_counts: dict[str, int] = {}

        # 全局限速
        self._global_last_consolidation: float = 0.0
        self._min_global_interval: float = 120.0

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

    async def destroy(self):
        """销毁调度器"""
        self._destroyed = True
        pending_tasks = []
        for t in (self._flush_task, self._idle_check_task):
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

    async def on_message(self, user_id: str, conversation_text: str, sender_name: str = ""):
        """
        每次消息调用：计数 → 判断是否触发 → 触发则入队 WarmProcessor

        所有重量操作都在后台队列中执行，本方法不阻塞。
        """
        if self._destroyed:
            return

        # 1. 内存计数（零 SQLite 开销）
        self._pending_counts[user_id] = self._pending_counts.get(user_id, 0) + 1
        self._last_activity[user_id] = time.time()

        # 2. 去抖
        now = time.time()
        last_check = self._last_trigger_check.get(user_id, 0.0)
        if now - last_check < self._debounce_interval:
            return
        self._last_trigger_check[user_id] = now

        # 3. 内存计数 → 刷入状态
        state = self._get_or_create_state(user_id)
        state.msg_count += self._pending_counts.pop(user_id, 0)
        self._mark_dirty(user_id)

        # 4. 全局限速：距上次整理不足 2 分钟则跳过
        if self._global_last_consolidation > 0 and now - self._global_last_consolidation < self._min_global_interval:
            logger.debug(f"[Memory] 全局限速: 距上次 {now - self._global_last_consolidation:.0f}s")
            return

        # 5. 检查触发条件
        should_trigger = False
        trigger_reason = ""

        # A. 条数阈值（含暖启动）
        threshold = state.warmup_threshold if (self.warmup_enabled and state.warmup_threshold > 0) else self.trigger_msg_count
        if state.msg_count >= threshold:
            should_trigger = True
            trigger_reason = f"消息条数达到 {threshold}"

        # B. 时间间隔
        if not should_trigger and self.trigger_time_minutes > 0:
            elapsed = now - state.last_consolidated_at
            if elapsed >= self.trigger_time_minutes * 60:
                should_trigger = True
                trigger_reason = f"时间间隔达到 {self.trigger_time_minutes} 分钟"

        if not should_trigger:
            return

        # 6. 触发 → 入队 WarmProcessor（非阻塞）
        logger.info(f"[Memory] 触发整理: uid={user_id} {trigger_reason}")
        if self.warm_processor:
            await self.warm_processor.enqueue(user_id, conversation_text, state, sender_name, on_done=self._after_consolidation)

    # ── 整理完成回调（由 WarmProcessor 执行完毕后调用） ──

    async def _after_consolidation(self, user_id: str, result):
        """整理后的收尾工作 — 状态更新 + 暖启动 + 标记脏"""
        self._global_last_consolidation = time.time()
        state = self._get_or_create_state(user_id)
        state.reset_after_consolidation()

        # 暖启动：阈值指数增长（封顶 trigger_msg_count）
        if self.warmup_enabled and state.warmup_threshold > 0:
            new_threshold = min(state.warmup_threshold * 2, self.trigger_msg_count)
            state.warmup_threshold = 0 if new_threshold >= self.trigger_msg_count else new_threshold

        self._mark_dirty(user_id)

    # ── 延迟刷写 ──

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

    # ── 空闲检测 ──

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
                    pending = self._pending_counts.pop(uid, 0)
                    if pending and state:
                        state.msg_count += pending
                    if state and state.msg_count > 0 and self.warm_processor:
                        logger.info(f"[Memory] 空闲超时触发: {uid}")
                        await self.warm_processor.enqueue(uid, "", state, on_done=self._after_consolidation)
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    # ── 状态管理 ──

    def _get_or_create_state(self, user_id: str) -> PersistedSessionState:
        if user_id not in self._states:
            self._states[user_id] = PersistedSessionState(user_id=user_id)
        return self._states[user_id]

    def get_state(self, user_id: str) -> PersistedSessionState | None:
        return self._states.get(user_id)

    def set_warm_processor(self, warm_processor: IWarmProcessor):
        """注入 WarmProcessor（初始化顺序解耦）"""
        self.warm_processor = warm_processor

    def update_config(self, config: dict[str, Any]):
        """热更新配置 — 替代外部直接写内部属性"""
        self.trigger_msg_count = config.get("trigger_msg_count", self.trigger_msg_count)
        self.trigger_time_minutes = config.get("trigger_time_minutes", self.trigger_time_minutes)
        self.warmup_enabled = config.get("warmup_enabled", self.warmup_enabled)
