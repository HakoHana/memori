"""Retriever 检索引擎测试

测试分层：
1. RRF 融合算法正确性
2. BM25Retriever 检索逻辑
3. GraphEntityRetriever 检索逻辑
4. Retriever 整体集成
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call

import pytest

from memori.models.memory_atom import MemoryAtom, AtomType, RecallResult
from memori.retrieval.rrf_fusion import rrf_merge
from memori.retrieval.bm25_retriever import BM25Retriever


class TestRRFFusion:
    """RRF 融合算法"""

    RRF_K = 60

    def test_empty_input(self):
        assert rrf_merge([], top_k=5) == []
        assert rrf_merge([[]], top_k=5) == []

    def test_single_list(self):
        atoms = [
            MemoryAtom(user_id="u1", diary_date="d", content="A", atom_id=1),
            MemoryAtom(user_id="u1", diary_date="d", content="B", atom_id=2),
        ]
        result = rrf_merge([atoms], top_k=5)
        assert len(result) == 2
        assert result[0].atom_id == 1  # A 排名更高

    def test_multi_list_interleaving(self):
        """同时出现在多列表中的原子应获得更高 RRF 分数"""
        atom1 = MemoryAtom(user_id="u1", diary_date="d", content="A", atom_id=1)
        atom2 = MemoryAtom(user_id="u1", diary_date="d", content="B", atom_id=2)

        # A 在两个列表中都出现，B 只出现在一个中
        result = rrf_merge([[atom1, atom2], [atom1]], top_k=5)
        assert result[0].atom_id == 1  # A 排在 B 前面

    def test_top_k_respected(self):
        atoms = [
            MemoryAtom(user_id="u1", diary_date="d", content=f"原子{i}", atom_id=i)
            for i in range(10)
        ]
        result = rrf_merge([atoms], top_k=3)
        assert len(result) == 3

    def test_deduplication(self):
        """RRF 应去重（同一原子在不同列表中只出现一次）"""
        atom = MemoryAtom(user_id="u1", diary_date="d", content="重复", atom_id=1)
        result = rrf_merge([[atom], [atom]], top_k=5)
        assert len(result) == 1


class TestBM25Retriever:
    """BM25Retriever 文档路检索"""

    @pytest.fixture
    def atom_store(self):
        store = MagicMock()
        store.search_fts = AsyncMock(return_value=[])
        store.fetch = AsyncMock(return_value=[])
        store._row_to_atom = MagicMock()
        return store

    @pytest.fixture
    def bm25(self, atom_store):
        return BM25Retriever(atom_store=atom_store)

    async def test_empty_keywords(self, bm25):
        result = await bm25.retrieve([], ["user1"], k=5)
        assert result == []

    async def test_empty_user_ids(self, bm25):
        result = await bm25.retrieve(["test"], [], k=5)
        assert result == []

    async def test_ascii_keywords_call_fts(self, bm25, atom_store):
        await bm25.retrieve(["hello", "world"], ["user1"], k=5)
        atom_store.search_fts.assert_awaited_once()
        # 验证 FTS 查询用的是 OR 连接
        query_arg = atom_store.search_fts.await_args.kwargs["query"]
        assert "OR" in query_arg

    async def test_cjk_keywords_call_like(self, bm25, atom_store):
        await bm25.retrieve(["测试", "框架"], ["user1"], k=5)
        atom_store.fetch.assert_awaited()
        # 至少有一次 fetch 调用（LIKE 查询）
        assert atom_store.fetch.await_count >= 1

    async def test_short_keywords_filtered(self, bm25, atom_store):
        """长度不足2的关键词应被过滤"""
        await bm25.retrieve(["a", "b"], ["user1"])
        atom_store.search_fts.assert_not_awaited()


class TestRetriever:
    """Retriever 整体集成测试"""

    @pytest.fixture
    def mock_stores(self):
        return {
            "atom_store": MagicMock(),
            "persona_store": MagicMock(),
            "diary_store": MagicMock(),
            "graph_store": None,  # 无图路 → 仅 BM25
        }

    @pytest.fixture
    def retriever(self, mock_stores):
        from memori.core.retriever import Retriever

        mock_stores["atom_store"].search_fts = AsyncMock(return_value=[])
        mock_stores["atom_store"].fetch = AsyncMock(return_value=[])
        mock_stores["atom_store"].get_related_user_ids = AsyncMock(return_value=[])
        mock_stores["atom_store"].touch = AsyncMock()
        mock_stores["atom_store"]._row_to_atom = MagicMock()

        mock_stores["persona_store"].read = AsyncMock(return_value=None)

        return Retriever(**mock_stores)

    def test_implements_interface(self, retriever):
        from memori.core.interfaces import IRetriever
        assert isinstance(retriever, IRetriever)

    async def test_recall_empty_query(self, retriever):
        result = await retriever.recall("user1", "", k=5)
        assert result == []

    async def test_recall_no_results(self, retriever):
        result = await retriever.recall("user1", "一些关键词", k=5)
        assert isinstance(result, list)

    async def test_get_context_memories_without_persona(self, retriever):
        retriever.persona_store.read = AsyncMock(return_value=None)
        result = await retriever.get_context_memories("user1", "查询", k=3)
        assert isinstance(result, RecallResult)
        assert result.persona_text is None

    async def test_get_context_memories_with_persona(self, retriever):
        retriever.persona_store.read = AsyncMock(return_value="用户画像内容")
        retriever.atom_store.search_fts = AsyncMock(return_value=[])
        retriever.atom_store.fetch = AsyncMock(return_value=[])
        result = await retriever.get_context_memories("user1", "查询", k=3)
        assert isinstance(result, RecallResult)
        assert result.persona_text is not None

    async def test_search_diaries_no_store(self, retriever):
        retriever.diary_store = None
        result = await retriever.search_diaries("user1", "查询", k=5)
        assert result == []

    async def test_hybrid_search(self, retriever):
        retriever.diary_store = MagicMock()
        retriever.diary_store.search_fts = AsyncMock(return_value=[])
        retriever.atom_store.search_fts = AsyncMock(return_value=[])
        retriever.atom_store.fetch = AsyncMock(return_value=[])
        result = await retriever.hybrid_search("user1", "查询", k=3)
        assert "atoms" in result
        assert "diaries" in result

    async def test_get_recent_context_from_cache(self, retriever):
        retriever.hot_cache = MagicMock()
        retriever.hot_cache.format_recent_context = MagicMock(return_value="缓存内容")
        result = await retriever.get_recent_context("user1")
        assert result == "缓存内容"

    async def test_get_recent_context_db_fallback(self, retriever):
        retriever.hot_cache = MagicMock()
        retriever.hot_cache.format_recent_context = MagicMock(return_value="")
        retriever.conversation_store = MagicMock()
        retriever.conversation_store.get_recent_context = AsyncMock(return_value="历史内容")
        result = await retriever.get_recent_context("user1", session_id="s1")
        assert result == "历史内容"

    async def test_keyword_list(self, retriever):
        keywords = retriever._keyword_list("用户完成了测试框架搭建")
        assert len(keywords) > 0
        assert "完成" in keywords or "测试框架" in keywords or "搭建" in keywords

    def test_segment(self, retriever):
        tokens = retriever._segment("hello world 测试")
        assert len(tokens) >= 3
        assert "hello" in tokens
        assert "测试" in tokens

    def test_rrf_merge_static(self, retriever):
        from memori.core.retriever import Retriever as R
        atom1 = MemoryAtom(user_id="u1", diary_date="d", content="A", atom_id=1)
        atom2 = MemoryAtom(user_id="u1", diary_date="d", content="B", atom_id=2)
        result = R._rrf_merge([[atom1, atom2], [atom1]], k=5)
        assert result[0].atom_id == 1
