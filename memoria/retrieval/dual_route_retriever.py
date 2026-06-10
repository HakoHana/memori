"""双路检索协调器 — 文档路 + 图路 + RRF 融合"""

from __future__ import annotations

from typing import Any

from ..models.memory_atom import MemoryAtom
from ..core.logger import logger

from .bm25_retriever import BM25Retriever
from .graph_entity_retriever import GraphEntityRetriever
from .rrf_fusion import rrf_merge


class DualRouteRetriever:
    """双路检索引擎

    协调 BM25 文档路 + Graph 图路，RRF 融合排序。
    """

    def __init__(
        self,
        bm25_retriever: BM25Retriever,
        graph_retriever: GraphEntityRetriever,
    ):
        self.bm25 = bm25_retriever
        self.graph = graph_retriever

    async def retrieve(
        self,
        keywords: list[str],
        user_ids: list[str],
        k: int = 5,
    ) -> list[MemoryAtom]:
        """双路检索入口

        1. BM25 文档路 → from memory_atoms_fts
        2. Graph 图路   → entity→graph→diary→fact
        3. RRF 融合结果
        """
        if not keywords or not user_ids:
            return []

        logger.debug(f"[DualRoute] keywords={keywords} users={user_ids} top_k={k}")

        # 1. 文档路（多取一些候选供 RRF 排序）
        doc_results = await self.bm25.retrieve(keywords, user_ids, k=k * 3)

        # 2. 图路
        graph_results = await self.graph.retrieve(keywords, user_ids, k=k * 2)

        logger.debug(
            f"[DualRoute] bm25={len(doc_results)} graph={len(graph_results)}"
        )

        # 3. RRF 融合（凑不足时由一路兜底）
        all_lists = [lst for lst in [doc_results, graph_results] if lst]
        if not all_lists:
            return []

        fused = rrf_merge(all_lists, top_k=k)
        return fused
