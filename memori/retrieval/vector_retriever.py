"""向量路检索器 — 嵌入语义搜索"""

from __future__ import annotations

from typing import Any

from ..models.memory_atom import MemoryAtom
from ..core.adapters import EmbeddingProvider
from ..storage.atom_store import AtomStore


class VectorRetriever:
    """向量检索器（语义搜索）

    作为第三路加入 MultiRouteRetriever，
    无 EmbeddingProvider 时自动降级（返回空列表）。

    使用方式:
        from memori.retrieval.vector_retriever import VectorRetriever
        vr = VectorRetriever(atom_store, embed_provider)
        results = await vr.retrieve(["关键词"], ["user1"], k=5)
    """

    def __init__(
        self,
        atom_store: AtomStore,
        embed_provider: EmbeddingProvider | None = None,
    ):
        self.atom_store = atom_store
        self.embed = embed_provider

    async def retrieve(
        self,
        keywords: list[str],
        user_ids: list[str],
        k: int = 5,
    ) -> list[MemoryAtom]:
        """向量检索入口

        1. 关键词拼接为 query
        2. EmbeddingProvider 计算 query 向量
        3. AtomStore.search_vector 余弦相似度排序

        Args:
            keywords: 关键词列表
            user_ids: 用户 ID 列表，为空 = 全库搜索
            k: 返回 top N

        Returns:
            按相似度降序排列的 MemoryAtom 列表
        """
        if not self.embed or not keywords:
            return []

        query = " ".join(keywords)
        query_embed = await self.embed.embed(query)
        uid = user_ids[0] if user_ids else None
        return await self.atom_store.search_vector(
            query_embed, uid, k=k,
            model_name=type(self.embed).__name__,
        )
