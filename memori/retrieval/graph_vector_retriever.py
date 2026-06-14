"""图路向量检索器 — embedding 语义搜索图节点 → 沿边找关联日记 → 原子"""

from __future__ import annotations

from typing import Any

from ..models.memory_atom import MemoryAtom
from ..core.logger import logger
from .graph_utils import fetch_atoms_from_diaries, find_linked_diaries


class GraphVectorRetriever:
    """图路向量检索器

    流水线：
    1. query → embedding
    2. nodes 表向量搜索 → (node_id TEXT, similarity)
    3. 每个匹配节点 → edges.mentions → diary_ids
    4. fetch_atoms_from_diaries(score_multiplier=similarity)
    5. 收集、去重、按加权分数排序、取 top-k
    """

    def __init__(
        self,
        graph_store: Any,
        atom_store: Any,
        embed_provider: Any | None = None,
        k_per_node: int = 3,
    ):
        self.graph_store = graph_store
        self.atom_store = atom_store
        self.embed = embed_provider
        self.k_per_node = k_per_node

    async def retrieve(
        self,
        keywords: list[str],
        user_ids: list[str],
        k: int = 5,
    ) -> list[MemoryAtom]:
        if not self.embed or not keywords:
            return []

        query = " ".join(keywords)
        query_embed = await self.embed.embed(query)

        # 1. 向量搜索图节点（新表 nodes，返回 TEXT ID）
        model_name = type(self.embed).__name__
        vector_results = await self.graph_store.search_vector(
            query_embed, k=k * 3, model_name=model_name,
        )
        if not vector_results:
            return []

        # 2. 每个节点 → diary_ids → 加权原子
        collected: dict[int, tuple[MemoryAtom, float]] = {}

        for node_id, similarity in vector_results:
            diary_ids = await find_linked_diaries(self.graph_store, [node_id])
            if not diary_ids:
                continue

            atoms = await fetch_atoms_from_diaries(
                self.atom_store, diary_ids, user_ids,
                k=self.k_per_node, score_multiplier=similarity,
            )
            for a in atoms:
                effective = similarity * a.importance
                if a.atom_id not in collected or effective > collected[a.atom_id][1]:
                    collected[a.atom_id] = (a, effective)

        if not collected:
            return []

        # 3. 按有效分数降序取 top-k
        sorted_atoms = [a for a, _ in sorted(collected.values(), key=lambda x: -x[1])]
        return sorted_atoms[:k]
