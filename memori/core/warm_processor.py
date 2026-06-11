"""异步队列消费者 — 所有重量级操作走后台队列，不阻塞实时回复"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from .logger import logger

from ..models.memory_atom import CaptureResult


class WarmProcessor:
    """
    后台暖处理队列

    消费 ConsolidationManager 触发的整理任务，流水线：
    should_capture → capture (add_diary + extract_facts) → index_diary

    所有 LLM 调用和数据库写入都在 Worker 中异步执行，
    不阻塞消息的实时回复路径。
    """

    def __init__(
        self,
        capturer=None,
        graph_engine=None,
        persona_engine=None,
        config: dict[str, Any] | None = None,
    ):
        self.capturer = capturer
        self.graph_engine = graph_engine
        self.persona_engine = persona_engine
        self.config = config or {}

        # 队列配置
        self.max_retries = config.get("max_l1_retries", 3)
        self.persona_update_interval = config.get("persona_update_interval", 10)

        # 异步队列
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None
        self._destroyed = False

        # 速率限制（每个用户的处理间隔）
        self._min_user_interval: float = 60.0
        self._last_process: dict[str, float] = {}

    # ── 生命周期 ──

    async def start(self):
        """启动队列消费者"""
        self._worker_task = asyncio.create_task(self._worker_loop())
        logger.info("[WarmProcessor] 后台队列已启动")

    async def stop(self):
        """停止队列消费者"""
        self._destroyed = True
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except (asyncio.CancelledError, Exception):
                pass

    # ── 入队 ──

    async def enqueue(
        self,
        user_id: str,
        conversation_text: str,
        state,
        sender_name: str = "",
        on_done: Callable | None = None,
    ):
        """将一次整理任务加入后台队列

        Args:
            user_id: 用户 ID
            conversation_text: 待处理的对话文本
            state: PersistedSessionState 对象（用于回调后状态更新）
            sender_name: 发送者昵称（内部流转用 uid，给 LLM 前用此值）
            on_done: 处理完成后的回调 (user_id, result) -> None
        """
        await self._queue.put({
            "user_id": user_id,
            "text": conversation_text,
            "state": state,
            "sender_name": sender_name,
            "on_done": on_done,
        })

    # ── 队列消费者 ──

    async def _worker_loop(self):
        """后台消费者：逐条处理队列任务"""
        while not self._destroyed:
            try:
                task = await self._queue.get()
                user_id = task["user_id"]

                # 用户级速率限制
                now = time.time()
                last = self._last_process.get(user_id, 0.0)
                if now - last < self._min_user_interval:
                    logger.debug(f"[WarmProcessor] {user_id} 距上次处理 {now - last:.0f}s 不足 {self._min_user_interval}s，跳过")
                    self._queue.task_done()
                    continue

                await self._process_one(task)
                self._last_process[user_id] = time.time()
                self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[WarmProcessor] worker 异常: {e}")
                self._queue.task_done()

    async def _process_one(self, task: dict):
        """执行一次完整整理流水线"""
        user_id = task["user_id"]
        text = task["text"]
        state = task.get("state")
        on_done = task.get("on_done")

        # 1. 为对话附加用户身份标签（让 LLM 能分清谁说了什么）
        sender_name = task.get("sender_name", "")
        tagged = await self._tag_conversation(user_id, text, sender_name)

        # 2. Judge — LLM 判断值不值得记
        judge = await self._judge(tagged)
        if not judge or not judge.should_remember:
            # 不值得记也要更新最后整理时间，避免无限重试
            if on_done:
                try:
                    result = CaptureResult(wrote_diary=False)
                    await on_done(user_id, result)
                except Exception:
                    pass
            return

        # 3. ★ 提前去重 — 与已有记忆重复则强化并跳过昂贵模型
        if self.capturer:
            try:
                matched, ex = await self.capturer._apply_reinforcement(
                    content=tagged,
                    user_id=user_id,
                    judge_importance=judge.importance,
                    threshold=0.85,
                )
                if matched:
                    logger.info(
                        f"[WarmProcessor] 提前去重命中，跳过 Capture: "
                        f"uid={user_id} matched_id={ex.atom_id if ex else '?'} "
                        f"content={tagged[:60]}"
                    )
                    if on_done:
                        try:
                            result = CaptureResult(wrote_diary=False)
                            await on_done(user_id, result)
                        except Exception:
                            pass
                    return
            except Exception as e:
                logger.warning(f"[WarmProcessor] 提前去重异常（忽略，继续 Capture）: {e}")

        # 4. Capture — 写日记 + 提取原子 + 更新图谱
        result = await self._capture_with_retry(user_id, tagged, judge)

        # 5. Persona 更新（L3）
        if result.wrote_diary and state:
            try:
                await self._maybe_update_persona(user_id, state)
            except Exception as e:
                logger.warning(f"[WarmProcessor] L3 画像更新失败: {e}")

        # 6. 回调通知 ConsolidationManager 更新状态
        if on_done:
            try:
                await on_done(user_id, result)
            except Exception as e:
                logger.warning(f"[WarmProcessor] 回调异常: {e}")

    # ── 流水线各步骤 ──

    async def _tag_conversation(self, user_id: str, text: str, sender_name: str = "") -> str:
        """给对话文本附加用户显示名（内部 uid 流转，给 LLM 前换为昵称）"""
        if not text or text.startswith("["):
            return text
        display_name = sender_name or user_id
        return f"[{display_name}]: {text}"

    async def _judge(self, tagged_text: str):
        """Step 1: Judge — LLM 判断"""
        if not self.capturer:
            return None
        try:
            return await self.capturer.should_capture(tagged_text)
        except Exception as e:
            logger.warning(f"[WarmProcessor] Judge 失败: {e}")
            return None

    async def _capture_with_retry(self, user_id: str, text: str, judge) -> CaptureResult:
        """Step 2: Capture — 写日记 + 提取原子（带重试）"""
        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                return await self.capturer.capture(user_id, text, judge)
            except Exception as e:
                last_error = e
                if attempt < self.max_retries:
                    delay = min(2 ** attempt * 5, 30)
                    logger.warning(f"[WarmProcessor] Capture 重试 {attempt+1}/{self.max_retries}: {e}, 等待 {delay}s")
                    await asyncio.sleep(delay)
        logger.error(f"[WarmProcessor] Capture 全部重试失败: {last_error}")
        return CaptureResult(wrote_diary=False)

    async def _maybe_update_persona(self, user_id: str, state):
        """Step 3: 画像更新（L3）"""
        if not self.persona_engine or not hasattr(state, 'diary_count_since_persona'):
            return
        if state.diary_count_since_persona < self.persona_update_interval:
            return
        try:
            ok = await self.persona_engine.incremental_update(user_id)
            if not ok:
                await self.persona_engine.full_rebuild(user_id)
            state.diary_count_since_persona = 0
            logger.info(f"[WarmProcessor] 画像已更新: {user_id} (增量={ok})")
        except Exception as e:
            logger.warning(f"[WarmProcessor] L3 失败: {e}")

    # ── 外部注入 ──

    def set_capturer(self, capturer):
        self.capturer = capturer

    def set_graph_engine(self, graph_engine):
        self.graph_engine = graph_engine

    def set_persona_engine(self, persona_engine):
        self.persona_engine = persona_engine

    @property
    def queue_size(self) -> int:
        """当前队列积压数"""
        return self._queue.qsize()
