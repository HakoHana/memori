"""检索引擎 — 召回相关记忆"""

from __future__ import annotations

from typing import Any

from ..models.memory_atom import MemoryAtom, RecallResult
from ..storage.atom_store import AtomStore
from ..storage.persona_store import PersonaStore


class Retriever:
    """
    记忆检索引擎

    MVP 使用 FTS5 全文搜索，预留向量搜索接口。
    """

    def __init__(
        self,
        atom_store: AtomStore,
        persona_store: PersonaStore,
        config: dict[str, Any] | None = None,
    ):
        self.atom_store = atom_store
        self.persona_store = persona_store
        self.config = config or {}
        self.recall_count = self.config.get("recall_count", 5)
        self.recall_max_tokens = self.config.get("recall_max_tokens", 500)

    def _search_weights(self) -> tuple[float, float]:
        """从配置读取搜索权重"""
        imp = float(self.config.get("search_imp_weight", 0.6))
        rank = float(self.config.get("search_rank_weight", 0.4))
        total = imp + rank
        if total <= 0:
            return 0.6, 0.4
        return imp / total, rank / total  # 归一化，确保加起来=1

    async def recall(self, user_id: str, query: str, k: int | None = None) -> list[MemoryAtom]:
        """搜索相关记忆原子"""
        k = k or self.recall_count
        if not query or not query.strip():
            return []

        imp_w, rank_w = self._search_weights()
        atoms = await self.atom_store.search_fts(query, user_id, k, imp_w, rank_w)
        return atoms

    async def get_context_memories(
        self, user_id: str, query: str, k: int | None = None
    ) -> RecallResult:
        """
        生成供注入用的记忆文本

        返回 RecallResult，包含格式化的文本 + 原子列表 + 画像
        """
        k = k or self.recall_count
        atoms = await self.recall(user_id, query, k)
        persona = await self.persona_store.read(user_id)

        # 更新原子的访问时间
        for atom in atoms:
            await self.atom_store.touch(atom.atom_id)

        # 组装文本
        lines = []

        # 画像部分
        if persona:
            lines.append(f"【关于你】\n{persona[:300]}")

        # 原子部分
        if atoms:
            parts = []
            for a in atoms:
                date_part = f" ({a.diary_date})" if a.diary_date else ""
                parts.append(f"- [{a.atom_type.value}]{date_part} {a.content[:200]}")
            if parts:
                lines.append("【我记忆中最近的事】")
                lines.extend(parts)

        # 检查 token 预算
        text = "\n".join(lines)
        # 简单的 token 估算（中文 ~1.5 字/token，英文 ~4 字/token）
        if len(text) > self.recall_max_tokens * 4:
            # 裁剪过长文本
            text = text[: self.recall_max_tokens * 4] + "..."

        return RecallResult(
            memory_text=text,
            atoms=atoms,
            persona_text=persona,
        )

    async def recall_by_keywords(
        self, user_id: str, keywords: list[str], k: int = 5
    ) -> list[MemoryAtom]:
        """按关键词列表搜索（多个关键词 OR 匹配）"""
        query = " ".join(keywords)
        return await self.recall(user_id, query, k)
