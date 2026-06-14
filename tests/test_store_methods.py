"""新增 Store 方法测试 — DiaryStore / AtomStore / GraphStore 通用查询"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
class TestDiaryStoreListPaginated:
    """DiaryStore.list_paginated 分页查询"""

    @pytest.fixture
    async def store(self):
        from memori.storage.diary_store import DiaryStore
        s = DiaryStore(db_path=":memory:")
        await s.initialize()
        for i in range(10):
            await s.append(f"2026-06-{i+1:02d}", f"diary content {i}")
        return s

    async def test_list_all(self, store):
        items, total = await store.list_paginated(page=1, size=20)
        assert len(items) == 10
        assert total == 10

    async def test_pagination(self, store):
        page1, total = await store.list_paginated(page=1, size=3)
        assert len(page1) == 3
        assert total == 10

        page2, total = await store.list_paginated(page=2, size=3)
        assert len(page2) == 3

        page4, total = await store.list_paginated(page=4, size=3)
        assert len(page4) == 1

    async def test_items_have_expected_fields(self, store):
        items, _ = await store.list_paginated(page=1, size=1)
        assert len(items) == 1
        item = items[0]
        assert "id" in item
        assert "date" in item
        assert "importance" in item
        assert "topics" in item


@pytest.mark.asyncio
class TestDiaryStoreGetById:
    """DiaryStore.get_by_id"""

    @pytest.fixture
    async def store(self):
        from memori.storage.diary_store import DiaryStore
        s = DiaryStore(db_path=":memory:")
        await s.initialize()
        await s.append("2026-06-13", "test content")
        row = await s.fetchone("SELECT id FROM diary_entries LIMIT 1")
        s._test_id = row[0] if row else 0
        return s

    async def test_get_existing(self, store):
        result = await store.get_by_id(store._test_id)
        assert result is not None
        assert result.get("user_id", "") == ""
        assert result["date"] == "2026-06-13"

    async def test_get_nonexistent(self, store):
        result = await store.get_by_id(99999)
        assert result is None

    async def test_get_zero(self, store):
        result = await store.get_by_id(0)
        assert result is None


@pytest.mark.asyncio
class TestDiaryStoreCount:
    """DiaryStore.count"""

    @pytest.fixture
    async def store(self):
        from memori.storage.diary_store import DiaryStore
        s = DiaryStore(db_path=":memory:")
        await s.initialize()
        for i in range(5):
            await s.append(f"2026-06-{i+1:02d}", f"content {i}")
        await s.append("2026-06-01", "b content")
        return s

    async def test_count_all(self, store):
        assert await store.count() == 6


@pytest.mark.asyncio
class TestDiaryStoreTimeline:
    """DiaryStore.get_timeline_dates"""

    @pytest.fixture
    async def store(self):
        from memori.storage.diary_store import DiaryStore
        s = DiaryStore(db_path=":memory:")
        await s.initialize()
        dates = ["2026-06-01", "2026-06-03", "2026-06-05", "2026-07-01", "2026-07-15"]
        for d in dates:
            await s.append(d, f"content {d}")
        return s

    async def test_all_dates(self, store):
        dates = await store.get_timeline_dates()
        assert len(dates) == 5

    async def test_filter_by_year(self, store):
        dates = await store.get_timeline_dates(year="2026")
        assert len(dates) == 5

    async def test_filter_by_year_month(self, store):
        dates = await store.get_timeline_dates(year="2026", month="6")
        assert len(dates) == 3
        assert all("06-" in d for d in dates)

    async def test_filter_no_results(self, store):
        dates = await store.get_timeline_dates(year="2025")
        assert len(dates) == 0


@pytest.mark.asyncio
class TestGraphStoreOverviewStats:
    """GraphStore.get_overview_stats"""

    @pytest.fixture
    async def store(self):
        from memori.storage.graph_store import GraphStore
        from memori.models.graph_models import GraphNode
        s = GraphStore(db_path=":memory:")
        await s.initialize()
        await s.upsert_nodes([
            GraphNode("entity", "alice", "alice"),
            GraphNode("entity", "bob", "bob"),
            GraphNode("topic", "coffee", "coffee"),
        ])
        return s

    async def test_overview_stats(self, store):
        stats = await store.get_overview_stats()
        assert "nodes" in stats
        assert "edges" in stats
        assert stats["nodes"].get("entity") == 2
        assert stats["nodes"].get("topic") == 1

    async def test_empty_store(self):
        """空 store 返回空 dict（用唯一路径避免连接池复用）"""
        from memori.storage.graph_store import GraphStore
        import uuid
        empty = GraphStore(db_path=f":memory:{uuid.uuid4().hex}")
        await empty.initialize()
        stats = await empty.get_overview_stats()
        assert stats["nodes"] == {}
        assert stats["edges"] == {}


@pytest.mark.asyncio
class TestAtomStoreUserMethods:
    """AtomStore.list_users_with_persona / get_user_persona"""

    @pytest.fixture
    async def store(self):
        from memori.storage.atom_store import AtomStore
        s = AtomStore(db_path=":memory:")
        await s.initialize()
        # 需要先有 canonical_users 记录
        await s.execute(
            "INSERT INTO canonical_users (uid, primary_name) VALUES (?, ?)",
            ("u_alice", "Alice"),
        )
        await s.execute(
            "INSERT INTO canonical_users (uid, primary_name) VALUES (?, ?)",
            ("u_bob", "Bob"),
        )
        # user_persona 记录
        await s.execute("""
            INSERT INTO user_persona (uid, summary, tags, tier, created_at, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))
        """, ("u_alice", "Alice's persona summary", '["friendly", "helpful"]', "known"))
        return s

    async def test_list_users(self, store):
        users = await store.list_users_with_persona()
        assert len(users) >= 2
        alice = [u for u in users if u["uid"] == "u_alice"]
        assert len(alice) == 1
        assert alice[0]["name"] == "Alice"

    async def test_get_persona_existing(self, store):
        persona = await store.get_user_persona("u_alice")
        assert persona is not None
        assert persona["summary"] == "Alice's persona summary"

    async def test_get_persona_nonexistent(self, store):
        persona = await store.get_user_persona("u_nobody")
        assert persona is None
