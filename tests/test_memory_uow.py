"""MemoryUnitOfWork 门面测试 — 验证对下层的调用编排正确

测试目标：
1. 每个门面方法是否正确调用了对应的 store 方法
2. 调用参数是否正确传递
3. WriteOpLog 是否在适当条件下跳过（None 时）
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from memori.models.memory_atom import MemoryAtom, AtomType
from memori.pipeline.memory_uow import MemoryUnitOfWork


class TestMemoryUnitOfWorkDiary:
    """日记操作门面"""

    @pytest.fixture
    def uow(self):
        diary = MagicMock()
        diary.append = AsyncMock(return_value=42)
        diary.update_metadata = AsyncMock()
        atom = MagicMock()
        atom.fetchone = AsyncMock(return_value=None)
        atom.fetch = AsyncMock(return_value=[])
        atom.execute = AsyncMock()
        atom.search_fts = AsyncMock(return_value=[])
        atom.insert_many = AsyncMock(return_value=[1, 2])
        atom.ensure_fact = AsyncMock(return_value=1)
        atom.link_fact = AsyncMock()
        log = MagicMock()
        return MemoryUnitOfWork(diary_store=diary, atom_store=atom, write_op_log=log)

    async def test_append_diary(self, uow):
        diary_id = await uow.append_diary("user1", "2026-06-11", "日记内容")
        assert diary_id == 42
        uow._diary.append.assert_awaited_once_with("user1", "2026-06-11", "日记内容")

    async def test_update_diary_importance(self, uow):
        await uow.update_diary_importance("user1", "2026-06-11", 0.9)
        uow._diary.update_metadata.assert_awaited_once_with("user1", "2026-06-11", importance=0.9)


class TestMemoryUnitOfWorkAtomRead:
    """原子读取门面"""

    @pytest.fixture
    def uow(self):
        diary = MagicMock()
        atom = MagicMock()
        atom.fetchone = AsyncMock(return_value=(3,))
        atom.fetch = AsyncMock(return_value=[(1, "内容1"), (2, "内容2")])
        atom.search_fts = AsyncMock(return_value=[])
        atom.execute = AsyncMock()
        log = MagicMock()
        return MemoryUnitOfWork(diary_store=diary, atom_store=atom, write_op_log=log)

    async def test_count_active_atoms(self, uow):
        count = await uow.count_active_atoms(diary_id=42)
        assert count == 3
        uow._atom.fetchone.assert_awaited_once()
        sql = uow._atom.fetchone.await_args[0][0]
        assert "COUNT" in sql
        assert "diary_id=?" in sql

    async def test_search_fts(self, uow):
        await uow.search_fts("query", "user1", k=5)
        uow._atom.search_fts.assert_awaited_once_with("query", "user1", k=5)

    async def test_fetch_forgotten(self, uow):
        results = await uow.fetch_forgotten("user1", diary_id=42)
        assert len(results) == 2
        uow._atom.fetch.assert_awaited_once()

    async def test_fetch_atom_diary(self, uow):
        diary_id = await uow.fetch_atom_diary(atom_id=1)
        assert diary_id == 3
        uow._atom.fetchone.assert_awaited_once()


class TestMemoryUnitOfWorkAtomWrite:
    """原子写入门面"""

    @pytest.fixture
    def uow(self):
        atom = MagicMock()
        atom.insert_many = AsyncMock(return_value=[10, 11])
        atom.ensure_fact = AsyncMock(return_value=1)
        atom.link_fact = AsyncMock()
        atom.execute = AsyncMock()
        atom.fetchone = AsyncMock(side_effect=[
            (5,),  # fetch_atom_diary 返回 diary_id=5
        ])
        diary = MagicMock()
        diary.append = AsyncMock()
        diary.update_metadata = AsyncMock()
        log = MagicMock()
        return MemoryUnitOfWork(diary_store=diary, atom_store=atom, write_op_log=log)

    async def test_insert_atoms(self, uow):
        atoms = [MemoryAtom(user_id="u1", diary_date="d", content="c")]
        ids = await uow.insert_atoms(atoms)
        assert ids == [10, 11]
        uow._atom.insert_many.assert_awaited_once_with(atoms)

    async def test_reinforce_atom(self, uow):
        await uow.reinforce_atom(atom_id=1, importance=0.9, confidence=0.95, expires_at=99999.0)
        # 验证 UPDATE memory_atoms
        update_call = uow._atom.execute.await_args_list[0]
        assert "UPDATE memory_atoms" in update_call[0][0]
        assert update_call[0][1] == (0.9, 0.95, 99999.0, 1)
        # 验证 diary_id 回写
        uow._atom.fetchone.assert_awaited_once()
        update_diary = uow._atom.execute.await_args_list[1]
        assert "UPDATE diary_entries" in update_diary[0][0]

    async def test_delete_forgotten_atom(self, uow):
        await uow.delete_forgotten_atom(atom_id=1)
        assert uow._atom.execute.await_count >= 2
        calls = uow._atom.execute.await_args_list
        assert any("DELETE FROM memory_atoms" in c[0][0] for c in calls)
        assert any("DELETE FROM memory_atoms_fts" in c[0][0] for c in calls)


class TestMemoryUnitOfWorkFacts:
    """事实表门面"""

    @pytest.fixture
    def uow(self):
        atom = MagicMock()
        atom.ensure_fact = AsyncMock(return_value=42)
        atom.link_fact = AsyncMock()
        diary = MagicMock()
        log = MagicMock()
        return MemoryUnitOfWork(diary_store=diary, atom_store=atom, write_op_log=log)

    async def test_ensure_fact(self, uow):
        fact_id = await uow.ensure_fact("内容", "episodic", 0.8, 0.9)
        assert fact_id == 42
        uow._atom.ensure_fact.assert_awaited_once_with("内容", "episodic", 0.8, 0.9)

    async def test_link_fact(self, uow):
        await uow.link_fact(1, 42, 0.8, "片段")
        uow._atom.link_fact.assert_awaited_once_with(1, 42, 0.8, "片段")


class TestMemoryUnitOfWorkWriteLog:
    """WriteOpLog 条件执行"""

    @pytest.fixture
    def uow_with_log(self):
        log = MagicMock()
        log.begin = AsyncMock(return_value="op_123")
        log.step = AsyncMock()
        log.complete = AsyncMock()
        return MemoryUnitOfWork(
            diary_store=MagicMock(),
            atom_store=MagicMock(),
            write_op_log=log,
        )

    @pytest.fixture
    def uow_without_log(self):
        return MemoryUnitOfWork(
            diary_store=MagicMock(),
            atom_store=MagicMock(),
            write_op_log=None,
        )

    async def test_begin_op_returns_id_when_log_exists(self, uow_with_log):
        op_id = await uow_with_log.begin_op("test_op", {"key": "val"})
        assert op_id == "op_123"
        uow_with_log._log.begin.assert_awaited_once_with("test_op", {"key": "val"})

    async def test_begin_op_returns_none_when_no_log(self, uow_without_log):
        op_id = await uow_without_log.begin_op("test_op", {"key": "val"})
        assert op_id is None

    async def test_step_op_noop_without_log(self, uow_without_log):
        # 不应抛出异常
        await uow_without_log.step_op(None, "step1")
        await uow_without_log.step_op("op_1", "step1")  # no log → should be noop

    async def test_complete_op_noop_without_log(self, uow_without_log):
        await uow_without_log.complete_op(None)
        await uow_without_log.complete_op("op_1")
