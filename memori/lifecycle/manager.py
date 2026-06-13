"""LifecycleManager — 记忆生命周期管理统一入口"""

from __future__ import annotations

from typing import Any

from ..core.logger import logger
from ..models.memory_atom import MemoryAtom

from .dedup import DedupEngine
from .decay import DecayEngine, compute_decay_score
from .cleanup import CleanupEngine
from .archiver import Archiver


class LifecycleManager:
    """记忆生命周期管理器

    统一管理记忆的完整生命周期：
    去重 → 强化 → 衰减 → 休眠 → 遗忘 → 归档 → 清理

    使用方式：
        lifecycle = LifecycleManager(atom_store, diary_store, embed_provider, config)
        await lifecycle.dedup_and_reinforce(content, user_id, ...)
        await lifecycle.run_daily_decay()
        await lifecycle.run_daily_archive()
        await lifecycle.run_daily_cleanup()

    各子引擎也可独立使用：
        await lifecycle.dedup.semantic_dedup(atoms, model_name)
        await lifecycle.decay.apply_global_decay(rate)
    """

    def __init__(
        self,
        atom_store,
        diary_store,
        embed_provider=None,
        config: dict[str, Any] | None = None,
    ):
        config = config or {}

        # 子引擎
        self.dedup = DedupEngine(atom_store, diary_store, config)
        self.decay = DecayEngine(atom_store, config)
        self.cleanup = CleanupEngine(atom_store, diary_store, config)

        # 归档模块（有条件初始化）
        self.archiver: Archiver | None = None
        archive_cfg = config.get("archive", {})
        if diary_store and archive_cfg.get("enabled", True):
            try:
                archive_path = config.get("archive_path", "./memory_archive")
                self.archiver = Archiver(
                    diary_store=diary_store,
                    archive_dir=archive_path,
                    config=config,
                )
            except Exception as e:
                logger.warning(f"[Lifecycle] 归档模块初始化失败: {e}")

        # 配置
        self._atom_store = atom_store
        self._diary_store = diary_store
        self._embed_provider = embed_provider
        self._config = config

    # ── 去重 + 强化 ──────────────────────────────────────

    async def dedup_and_reinforce(
        self,
        content: str,
        user_id: str,
        judge_importance: float = 0.5,
        new_confidence: float = 0.7,
        threshold: float = 0.6,
    ) -> tuple[bool, MemoryAtom | None]:
        """去重 + 强化（供 Capturer、WarmProcessor 调用）

        委托给 DedupEngine。
        """
        return await self.dedup.dedup_and_reinforce(
            content=content,
            user_id=user_id,
            judge_importance=judge_importance,
            new_confidence=new_confidence,
            threshold=threshold,
        )

    async def semantic_dedup(
        self,
        atoms: list[MemoryAtom],
        model_name: str,
        threshold: float = 0.92,
    ):
        """语义去重（嵌入计算后调用）

        委托给 DedupEngine。
        """
        await self.dedup.semantic_dedup(
            atoms=atoms,
            model_name=model_name,
            threshold=threshold,
        )

    async def cleanup_forgotten_duplicates(
        self,
        content: str,
        diary_id: int,
        user_ids: list[str],
        threshold: float = 0.6,
    ):
        """清理重复的已遗忘原子

        委托给 DedupEngine。
        """
        await self.dedup.cleanup_forgotten_duplicates(
            content=content,
            diary_id=diary_id,
            user_ids=user_ids,
            threshold=threshold,
        )

    # ── 日常任务 ─────────────────────────────────────────

    async def run_daily_decay(self):
        """每日衰减 + 过期清理（状态机接入前暂放于此）

        状态机接入后由此接口转交梦境状态机调度。
        """
        count = await self.decay.apply_global_decay()
        if count > 0:
            logger.info(f"[Lifecycle] 全局衰减完成: {count} 条")
        await self.run_daily_cleanup()

    async def scan_contradictions(self, user_id: str | None = None) -> list[dict]:
        """扫描矛盾记忆 — 接口预留供梦境状态机使用

        发现矛盾后 Bot 可在下次对话中提问澄清。
        当前为占位实现，状态机接入后重写此方法。

        Args:
            user_id: 指定用户，None=全库扫描

        Returns:
            [{"atom_a": ..., "atom_b": ..., "topic": "...", "conflict_type": "..."}, ...]
        """
        return []

    async def run_daily_archive(self):
        """每日归档（冷存储 → Markdown）"""
        if not self.archiver:
            return
        archived = await self.archiver.archive_daily()
        if archived:
            logger.info(f"[Lifecycle] 归档完成: {archived} 条")

    async def run_daily_cleanup(self):
        """每日清理：孤立原子 dormant + 过期原子硬删"""
        orphans = await self.cleanup.cleanup_orphans()
        expired = await self.cleanup.cleanup_expired()
        if orphans or expired:
            logger.info(
                f"[Lifecycle] 清理完成: {orphans} 条孤立 → dormant, "
                f"{expired} 条过期 → 删除"
            )

    # ── 统计 ─────────────────────────────────────────────

    async def run_daily_semantic_dedup(self):
        """每日语义去重（状态机接入前暂放于此）

        状态机接入后由此接口转交梦境状态机调度。
        """
        if not self._embed_provider:
            return
        try:
            marked = await self.dedup.scan_semantic_duplicates()
            if marked:
                logger.info(f"[Lifecycle] 语义去重完成: {marked} 条标记为 dormant")
        except Exception as e:
            logger.warning(f"[Lifecycle] 语义去重异常: {e}")

    async def get_stats(self, user_id: str | None = None) -> dict:
        """生命周期统计"""
        result = {}

        # 各状态分布
        for status in ("active", "dormant", "archived", "forgotten"):
            if user_id:
                row = await self._atom_store.fetchone(
                    "SELECT COUNT(*) FROM memory_atoms WHERE status=? AND user_id=?",
                    (status, user_id),
                )
            else:
                row = await self._atom_store.fetchone(
                    "SELECT COUNT(*) FROM memory_atoms WHERE status=?",
                    (status,),
                )
            result[f"{status}_count"] = row[0] if row else 0

        # 衰减进度
        total_active = result.get("active_count", 0)
        if total_active:
            result["decay_enabled"] = self._config.get("decay_enabled", True)
            result["decay_rate"] = float(self._config.get("decay_rate", 0.99))

        # 归档
        if self.archiver and self._diary_store:
            try:
                row = await self._diary_store.fetchone(
                    "SELECT COUNT(*) FROM diary_entries WHERE archived=1"
                )
                result["archived_diaries"] = row[0] if row else 0
            except Exception:
                pass

        return result
