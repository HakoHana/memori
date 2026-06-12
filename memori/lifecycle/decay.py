"""衰减引擎 — 衰减计算 + 全局衰减应用"""

from __future__ import annotations

from typing import Any

from ..models.memory_atom import DecayType, compute_decay_score


class DecayEngine:
    """衰减引擎

    职责：
    - 全局重要性衰减（每天对所有活跃原子 × rate）
    - 事实表同步衰减
    - 提供 compute_decay_score 静态方法
    """

    def __init__(self, atom_store, config: dict[str, Any] | None = None):
        self.atom_store = atom_store
        self.config = config or {}
        self._default_rate = float(self.config.get("decay_rate", 0.99))
        self._enabled = bool(self.config.get("decay_enabled", True))

    @staticmethod
    def compute_decay_score(
        decay_type: DecayType, ttl_days: float, age_days: float
    ) -> float:
        """静态衰减分数计算"""
        return compute_decay_score(decay_type, ttl_days, age_days)

    async def apply_global_decay(self, rate: float | None = None) -> int:
        """对所有活跃原子执行全局重要性衰减

        Args:
            rate: 衰减率（0.99 = 每天降 1%），默认使用配置值

        Returns:
            受影响的行数
        """
        rate = rate if rate is not None else self._default_rate
        if not self._enabled or rate <= 0 or rate >= 1.0:
            return 0

        cursor = await self.atom_store.execute(
            "UPDATE memory_atoms SET importance = importance * ? WHERE status = 'active'",
            (rate,),
        )
        count = cursor.rowcount if cursor else 0

        # 同步衰减事实表（保留低值底线 0.1）
        await self.atom_store.execute(
            f"UPDATE atomic_facts SET importance = importance * {rate} WHERE importance > 0.1"
        )

        return count
