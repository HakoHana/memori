"""清理引擎 — 孤立原子 + 过期原子清理"""

from __future__ import annotations

import time
from typing import Any

from ..core.logger import logger


class CleanupEngine:
    """清理引擎

    职责：
    - 孤立原子（低重要性 + 无对应日记）→ dormant 状态
    - 过期原子（dormant/forgotten 超过保留天数）→ 硬删除
    """

    def __init__(self, atom_store, diary_store=None, config: dict[str, Any] | None = None):
        self.atom_store = atom_store
        self.diary_store = diary_store
        self.config = config or {}
        self._orphan_importance = float(self.config.get("orphan_importance_threshold", 0.2))
        self._expired_ttl_days = float(self.config.get("expired_atom_ttl_days", 60))

    async def cleanup_orphans(self, importance_threshold: float | None = None) -> int:
        """将低重要性且无对应日记的原子标记为 dormant

        场景：日记被删除后，关联的低价值原子不应再参与召回。

        Args:
            importance_threshold: 重要性阈值，低于此值且无对应日记则标记 dormant。默认 0.2

        Returns:
            被标记为 dormant 的原子数
        """
        if not self.atom_store or not self.diary_store:
            return 0

        threshold = importance_threshold if importance_threshold is not None else self._orphan_importance

        rows = await self.atom_store.fetch(
            "SELECT DISTINCT diary_id FROM memory_atoms "
            "WHERE status='active' AND importance < ? AND diary_id > 0",
            (threshold,),
        )
        if not rows:
            return 0

        orphan_ids = []
        for (did,) in rows:
            row = await self.diary_store.fetchone(
                "SELECT 1 FROM diary_entries WHERE id=?", (did,)
            )
            if not row:
                orphan_ids.append(did)

        if not orphan_ids:
            return 0

        placeholders = ",".join("?" for _ in orphan_ids)
        cursor = await self.atom_store.execute(
            f"UPDATE memory_atoms SET status='dormant' "
            f"WHERE status='active' AND importance < ? AND diary_id IN ({placeholders})",
            (threshold, *orphan_ids),
        )
        count = cursor.rowcount if cursor else 0
        if count > 0:
            await self.atom_store.execute(
                "DELETE FROM memory_atoms_fts WHERE atom_id NOT IN "
                "(SELECT id FROM memory_atoms WHERE status IN ('active','dormant'))"
            )
            logger.info(f"[Lifecycle] 标记了 {count} 条孤立原子为 dormant")
        return count

    async def cleanup_expired(self, ttl_days: float | None = None) -> int:
        """硬删除超过保留天数的 dormant/forgotten 原子

        Args:
            ttl_days: 保留天数，默认 60

        Returns:
            删除的原子数
        """
        if not self.atom_store:
            return 0

        ttl = ttl_days if ttl_days is not None else self._expired_ttl_days
        cutoff = time.time() - ttl * 86400

        cursor = await self.atom_store.execute(
            "DELETE FROM memory_atoms WHERE status IN ('dormant','forgotten') AND created_at < ?",
            (cutoff,),
        )
        count = cursor.rowcount if cursor else 0
        if count > 0:
            await self.atom_store.execute(
                "DELETE FROM memory_atoms_fts WHERE atom_id NOT IN (SELECT id FROM memory_atoms)"
            )
            logger.info(f"[Lifecycle] 清理了 {count} 条过期原子 (>{ttl:.0f}天)")
        return count
