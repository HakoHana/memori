"""调度器 + 会话状态管理器 — 参考 TencentDB PipelineManager"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from ..logger import logger

from ..models.memory_atom import PersistedSessionState, CaptureResult
from ..storage.state_store import StateStore
from .capturer import Capturer
from .persona_engine import PersonaEngine


class ConsolidationManager:
    """
    调度器 + 会话状态管理器

    借鉴 TencentDB PipelineManager 设计：
    - L1 = Capturer（判断+写日记+提取原子）
    - L3 = PersonaEngine（画像更新）
    - 暖启动：新用户阈值从 1→2→4→8 指数增长
    - 空闲超时兜底
    - 会话状态持久化（带延迟写入去抖）

    优化：
    - 状态写入用延迟去抖（不是每次 on_message 都写）
    - 空闲定时器不随每条消息重启（用 check-interval 轮询）
    - 重试机制带指数退避
    """

    def __init__(
        self,
        capturer: Capturer,
        persona_engine: PersonaEngine,
        state_store: StateStore,
        on_memory_created: Callable | None = None,
        config: dict[str, Any] | None = None,
    ):
        self.capturer = capturer
        self.persona_engine = persona_engine
        self.state_store = state_store
        self.on_memory_created = on_memory_created
        self.config = config or {}

        # 配置
        self.trigger_msg_count = self.config.get("trigger_msg_count", 10)
        self.trigger_time_minutes = self.config.get("trigger_time_minutes", 360)
        self.immediate_capture = self.config.get("immediate_capture", True)
        self.warmup_enabled = self.config.get("warmup_enabled", True)
        self.idle_timeout_minutes = self.config.get("idle_timeout_minutes", 30)
        self.persona_update_interval = self.config.get("persona_update_interval", 10)
        self.max_l1_retries = self.config.get("max_l1_retries", 3)

        # 会话状态（内存中，延迟写回）
        self._states: dict[str, PersistedSessionState] = {}
        # 延迟写入追踪
        self._dirty_users: set[str] = set()
        self._flush_task: asyncio.Task | None = None
        self._flush_interval: float = 5.0  # 每 5 秒批量刷一次脏状态

        # 空闲检测：轮询任务，不随每条消息重启定时器
        self._idle_check_task: asyncio.Task | None = None
        self._idle_check_interval: float = 60.0  # 每 60 秒检查一次
        self._last_activity: dict[str, float] = {}

        self._destroyed = False

    async def initialize(self):
        """从数据库恢复所有会话状态"""
        states = await self.state_store.load_all()
        self._states = states
        now = time.time()
        for uid in states:
            if states[uid].msg_count > 0:
                self._last_activity[uid] = now

        # 启动延迟刷写任务
        self._flush_task = asyncio.create_task(self._flush_loop())
        # 启动空闲检测轮询
        self._idle_check_task = asyncio.create_task(self._idle_check_loop())

    async def destroy(self):
        """销毁调度器"""
        self._destroyed = True
        # 取消后台任务并等待完成
        pending_tasks = []
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
            pending_tasks.append(self._flush_task)
        if self._idle_check_task and not self._idle_check_task.done():
            self._idle_check_task.cancel()
            pending_tasks.append(self._idle_check_task)
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)

        # 强制刷写所有脏状态
        if self._dirty_users:
            await self._flush_dirty_states()

        # 持久化所有状态
        for uid, state in self._states.items():
            await self.state_store.save(state)
        self._states.clear()
        self._dirty_users.clear()

    async def on_message(self, user_id: str, conversation_text: str) -> CaptureResult | None:
        """
        每次消息调用：计数 + 判断是否触发

        优化：非触发类的消息仅更新脏标记，不立即写 DB
        """
        if self._destroyed:
            return None

        state = self._get_or_create_state(user_id)
        state.msg_count += 1
        self._mark_dirty(user_id)
        self._last_activity[user_id] = time.time()

        # 给对话文本附加用户身份，让 LLM 能分清谁说了什么
        if conversation_text and not conversation_text.startswith("["):
            tagged = f"[{user_id}]: {conversation_text}"
        else:
            tagged = conversation_text

        # 检查触发条件
        should_trigger = False
        trigger_reason = ""

        # A：消息数达到阈值
        threshold = (
            state.warmup_threshold
            if self.warmup_enabled and state.warmup_threshold > 0
            else self.trigger_msg_count
        )
        if state.msg_count >= threshold:
            should_trigger = True
            trigger_reason = f"消息数达到 {threshold}"

        # B：即时捕捉（重要事件）
        if self.immediate_capture and not should_trigger:
            try:
                judge = await self.capturer.should_capture(tagged)
                if judge.should_remember and judge.importance >= 0.7:
                    should_trigger = True
                    trigger_reason = f"即时捕捉: {judge.reason}"
                    result = await self.capturer.capture(user_id, tagged, judge)
                    await self._after_consolidation(user_id, result)
                    return result
            except Exception:
                pass

        # C：时间间隔
        if not should_trigger and self.trigger_time_minutes > 0:
            elapsed = time.time() - state.last_consolidated_at
            if elapsed >= self.trigger_time_minutes * 60:
                should_trigger = True
                trigger_reason = f"时间间隔达到 {self.trigger_time_minutes} 分钟"

        if not should_trigger:
            # 延迟写入会在 _flush_loop 中处理
            logger.debug(f"[Memory] 触发条件未满足: uid={user_id}, count={state.msg_count}/{threshold}")
            return None

        logger.info(f"[Memory] 触发整理: {trigger_reason}")

        # 执行 L1 整理
        judge = await self.capturer.should_capture(tagged)
        if not judge.should_remember:
            state.last_consolidated_at = time.time()
            state.l1_retry_count = 0
            self._mark_dirty(user_id)
            return None

        result = await self._run_l1_with_retry(user_id, tagged, judge)
        await self._after_consolidation(user_id, result)
        return result

    async def _run_l1_with_retry(self, user_id: str, conversation: str, judge) -> CaptureResult:
        """带重试的 L1 执行（指数退避）"""
        state = self._get_or_create_state(user_id)
        last_error = None

        for attempt in range(self.max_l1_retries + 1):
            try:
                result = await self.capturer.capture(user_id, conversation, judge)
                state.l1_retry_count = 0
                self._mark_dirty(user_id)
                return result
            except Exception as e:
                last_error = e
                state.l1_retry_count += 1
                self._mark_dirty(user_id)
                if attempt < self.max_l1_retries:
                    delay = min(2 ** attempt * 5, 30)  # 上限 30 秒
                    logger.warning(f"[Memory] L1 重试 {attempt+1}/{self.max_l1_retries}: {e}, 等待 {delay}s")
                    await asyncio.sleep(delay)

        logger.error(f"[Memory] L1 重试全部失败: {last_error}")
        return CaptureResult(wrote_diary=False)

    async def _after_consolidation(self, user_id: str, result: CaptureResult):
        """整理后的收尾工作"""
        state = self._get_or_create_state(user_id)
        state.reset_after_consolidation()

        # 暖启动：阈值指数增长
        if self.warmup_enabled and state.warmup_threshold > 0:
            new_threshold = min(state.warmup_threshold * 2, self.trigger_msg_count)
            state.warmup_threshold = 0 if new_threshold >= self.trigger_msg_count else new_threshold

        # 检查 L3（画像更新 — 优先增量，兜底全量）
        if state.diary_count_since_persona >= self.persona_update_interval:
            try:
                ok = await self.persona_engine.incremental_update(user_id)
                if not ok:
                    await self.persona_engine.full_rebuild(user_id)
                state.diary_count_since_persona = 0
                logger.info(f"[Memory] 画像已更新: {user_id} (增量={ok})")
            except Exception as e:
                logger.warning(f"[Memory] L3 画像更新失败: {e}")

        self._mark_dirty(user_id)

        # 通知外部
        if self.on_memory_created and result.wrote_diary:
            try:
                cb = self.on_memory_created
                if asyncio.iscoroutinefunction(cb):
                    await cb(user_id, result)
                else:
                    cb(user_id, result)
            except Exception:
                pass

    # ═══════════════════════════════════════════════════
    #  延迟刷写
    # ═══════════════════════════════════════════════════

    def _mark_dirty(self, user_id: str):
        """标记用户状态为脏（需要刷写）"""
        self._dirty_users.add(user_id)

    async def _flush_loop(self):
        """后台循环：定期刷写脏状态"""
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
        """批量刷写所有脏状态"""
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
    #  空闲检测（轮询模式，不依赖定时器重启）
    # ═══════════════════════════════════════════════════

    async def _idle_check_loop(self):
        """后台轮询：检查是否有用户超时未活动"""
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
                    if state and state.msg_count > 0:
                        logger.info(f"[Memory] 空闲超时触发: {uid}, 未处理消息: {state.msg_count}")
                        try:
                            judge = await self.capturer.should_capture("")
                            if judge.should_remember:
                                result = await self._run_l1_with_retry(uid, "", judge)
                                await self._after_consolidation(uid, result)
                        except Exception:
                            pass
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    # ═══════════════════════════════════════════════════
    #  状态管理
    # ═══════════════════════════════════════════════════

    def _get_or_create_state(self, user_id: str) -> PersistedSessionState:
        """获取或创建会话状态"""
        if user_id not in self._states:
            self._states[user_id] = PersistedSessionState(user_id=user_id)
        return self._states[user_id]

    def get_state(self, user_id: str) -> PersistedSessionState | None:
        """获取用户状态（供外部查询）"""
        return self._states.get(user_id)
