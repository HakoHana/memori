"""检索引擎 — 召回相关记忆（混合：原子 + 日记全文）"""

from __future__ import annotations

from typing import Any

from ..models.memory_atom import MemoryAtom, RecallResult
from ..storage.atom_store import AtomStore
from ..storage.diary_store import DiaryStore
from ..storage.persona_store import PersonaStore


class Retriever:
    """
    记忆检索引擎

    双通道检索：
    - 原子事实（atomic_facts 结构化匹配）
    - 日记全文（diary_fts FTS5）
    """

    def __init__(
        self,
        atom_store: AtomStore,
        persona_store: PersonaStore,
        diary_store: DiaryStore | None = None,
        config: dict[str, Any] | None = None,
    ):
        self.atom_store = atom_store
        self.persona_store = persona_store
        self.diary_store = diary_store
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
        """搜索相关记忆原子（FTS5 + LIKE 中文兜底）"""
        k = k or self.recall_count
        if not query or not query.strip():
            return []

        imp_w, rank_w = self._search_weights()
        atoms = await self.atom_store.search_fts(query, user_id, k, imp_w, rank_w)

        # FTS5 对中文支持有限，不足 k 条时用 LIKE 补充
        if len(atoms) < k:
            try:
                rows = await self.atom_store.fetch("""
                    SELECT * FROM memory_atoms
                    WHERE user_id=? AND status='active' AND content LIKE ?
                    ORDER BY importance DESC LIMIT ?
                """, (user_id, f"%{query}%", k - len(atoms)))
                seen_ids = {a.atom_id for a in atoms}
                for r in rows:
                    atom = self.atom_store._row_to_atom(r)
                    if atom.atom_id not in seen_ids:
                        atoms.append(atom)
            except Exception:
                pass

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

    async def search_diaries(self, user_id: str, query: str, k: int = 5) -> list[dict]:
        """搜索日记全文（diary_fts）"""
        if not self.diary_store:
            return []
        imp_w, rank_w = self._search_weights()
        return await self.diary_store.search_fts(query, user_id, k, imp_w, rank_w)

    async def hybrid_search(self, user_id: str, query: str, k: int = 5) -> dict:
        """混合搜索：原子 + 日记，合并返回"""
        atoms = await self.recall(user_id, query, k)
        diaries = await self.search_diaries(user_id, query, k)
        return {"atoms": atoms, "diaries": diaries}

    async def recall_by_keywords(
        self, user_id: str, keywords: list[str], k: int = 5
    ) -> list[MemoryAtom]:
        """按关键词列表搜索（多个关键词 OR 匹配）"""
        query = " ".join(keywords)
        return await self.recall(user_id, query, k)
