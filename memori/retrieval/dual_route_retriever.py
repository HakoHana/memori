"""多路检索编排 — 双路四模式混合检索（全库搜索，不限 user_id）

架构：
  MultiRouteRetriever.retrieve()
  ├── 文档路 (DocumentPath)
  │   ├── BM25Retriever.retrieve()       ← FTS5 + LIKE on memory_atoms
  │   └── VectorRetriever.retrieve()     ← embedding on memory_atoms
  │   └── 内部 RRF(2路, top_k=k) → doc_results
  ├── 图路 (GraphPath)
  │   ├── GraphKeywordRetriever.retrieve()  ← FTS5 + LIKE on graph_nodes
  │   └── GraphVectorRetriever.retrieve()   ← embedding on graph_nodes
  │   └── 内部 RRF(2路, top_k=k) → graph_results
  └── 跨路 RRF(文档路 + 图路, top_k=k) → 最终结果

向后兼容：
  DualRouteRetriever 保留为别名。
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..models.memory_atom import MemoryAtom
from ..core.logger import logger

from .bm25_retriever import BM25Retriever
from .graph_keyword_retriever import GraphKeywordRetriever
from .graph_vector_retriever import GraphVectorRetriever
from .vector_retriever import VectorRetriever
from .rrf_fusion import rrf_merge


class MultiRouteRetriever:
    """双路四模式混合检索引擎

    文档路（BM25 + 向量）+ 图路（关键词 + 向量），
    先内部 RRF 融合，再跨路 RRF 融合。
    """

    def __init__(
        self,
        bm25_retriever: BM25Retriever,
        graph_keyword_retriever: GraphKeywordRetriever,
        vector_retriever: VectorRetriever | None = None,
        graph_vector_retriever: GraphVectorRetriever | None = None,
    ):
        self.bm25 = bm25_retriever
        self.graph_kw = graph_keyword_retriever
        self.vec = vector_retriever
        self.graph_vec = graph_vector_retriever

    async def retrieve(
        self,
        keywords: list[str],
        user_ids: list[str],
        k: int = 5,
    ) -> list[MemoryAtom]:
        """双路四模式检索入口

        user_ids 为空 = 全库搜索（不限 user_id），子检索器已支持。
        候选数控制：
        - 子路检索取 k*2
        - 内部融合取 k
        - 跨路融合取 k
        """
        if not keywords:
            return []

        logger.debug(
            f"[MultiRoute] 双路四模式 keywords={keywords} users={user_ids} top_k={k}"
        )

        # ── 文档路：BM25 + Vector 并行 ──
        doc_tasks = [
            asyncio.create_task(self.bm25.retrieve(keywords, user_ids, k=k * 2)),
        ]
        if self.vec:
            doc_tasks.append(
                asyncio.create_task(self.vec.retrieve(keywords, user_ids, k=k * 2))
            )

        # ── 图路：GraphKeyword + GraphVector 并行 ──
        graph_tasks = [
            asyncio.create_task(self.graph_kw.retrieve(keywords, user_ids, k=k * 2)),
        ]
        if self.graph_vec:
            graph_tasks.append(
                asyncio.create_task(self.graph_vec.retrieve(keywords, user_ids, k=k * 2))
            )

        # ── 并发执行 ──
        doc_results = await asyncio.gather(*doc_tasks, return_exceptions=True)
        graph_results = await asyncio.gather(*graph_tasks, return_exceptions=True)

        # ── 文档路内部 RRF ──
        doc_lists: list[list[MemoryAtom]] = []
        for r in doc_results:
            if isinstance(r, list):
                doc_lists.append(r)
            elif isinstance(r, Exception):
                logger.warning(f"[MultiRoute] 文档一路异常: {r}")

        if not doc_lists:
            doc_fused = []
        else:
            doc_fused = rrf_merge(doc_lists, top_k=k)

        # ── 图路内部 RRF ──
        graph_lists: list[list[MemoryAtom]] = []
        for r in graph_results:
            if isinstance(r, list):
                graph_lists.append(r)
            elif isinstance(r, Exception):
                logger.warning(f"[MultiRoute] 图路一路异常: {r}")

        if not graph_lists:
            graph_fused = []
        else:
            graph_fused = rrf_merge(graph_lists, top_k=k)

        logger.debug(
            f"[MultiRoute] 文档路={len(doc_fused)} 图路={len(graph_fused)}"
        )

        # ── 跨路 RRF ──
        cross = [doc_fused, graph_fused]
        cross = [lst for lst in cross if lst]  # 过滤空
        if not cross:
            return []

        return rrf_merge(cross, top_k=k)


# ── 向后兼容别名 ──

class DualRouteRetriever(MultiRouteRetriever):
    """旧名称兼容 — 使用别名的代码继续工作"""

    def __init__(
        self,
        bm25_retriever: BM25Retriever,
        graph_keyword_retriever: GraphKeywordRetriever,
        vector_retriever: VectorRetriever | None = None,
        graph_vector_retriever: GraphVectorRetriever | None = None,
    ):
        import warnings
        warnings.warn(
            "DualRouteRetriever 已弃用，请使用 MultiRouteRetriever。"
            "DualRouteRetriever 的旧签名（仅 2 个参数）会静默禁用向量检索。",
            DeprecationWarning,
            stacklevel=2,
        )
        super().__init__(
            bm25_retriever=bm25_retriever,
            graph_keyword_retriever=graph_keyword_retriever,
            vector_retriever=vector_retriever,
            graph_vector_retriever=graph_vector_retriever,
        )
