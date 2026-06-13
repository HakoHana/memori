"""pytest 共享 fixture — mock store、mock LLM、示例数据"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from memori.models.memory_atom import (
    MemoryAtom,
    AtomType,
    AtomStatus,
    CaptureJudgeResult,
    CaptureResult,
    RecallResult,
)
from memori.pipeline.memory_uow import MemoryUnitOfWork


# ═══════════════════════════════════════════════════════════════
#  pytest-asyncio 配置
# ═══════════════════════════════════════════════════════════════

@pytest.fixture(scope="session")
def event_loop():
    """所有 async 测试共享一个 event loop（避免每个 fixture 创建/销毁 loop）"""
    import asyncio
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ═══════════════════════════════════════════════════════════════
#  模拟 LLM Provider
# ═══════════════════════════════════════════════════════════════

class MockLLMProvider:
    """可注入预设返回值的 LLMProvider 模拟"""

    def __init__(self, canned_responses: dict[str, str] | None = None):
        self.canned = canned_responses or {}
        self.call_log: list[tuple[str, str]] = []  # (system_prompt, user_prompt)

    async def chat(self, system_prompt: str, user_prompt: str, **kwargs) -> str:
        self.call_log.append((system_prompt, user_prompt))
        # 优先按精确匹配返回
        key = (system_prompt + user_prompt).strip()
        if key in self.canned:
            return self.canned[key]
        return self.canned.get("default", '{"should_remember": false}')

    async def chat_with_judge(self, system_prompt: str, user_prompt: str) -> str:
        return await self.chat(system_prompt, user_prompt)


@pytest.fixture
def mock_llm():
    """基础 mock LLM — 仅返回 should_remember=false"""
    return MockLLMProvider()


@pytest.fixture
def mock_llm_judge_positive():
    """mock LLM — judge 判定为值得记录"""
    return MockLLMProvider(canned_responses={
        "default": json.dumps({
            "should_remember": True,
            "reason": "用户提到了重要的工作进展",
            "importance": 0.8,
            "mood": "happy",
            "context_summary": "用户完成了项目里程碑",
        }),
    })


@pytest.fixture
def mock_llm_merged():
    """mock LLM — 合并调用返回日记+原子"""
    return MockLLMProvider(canned_responses={
        "default": json.dumps({
            "diary": "今天完成了测试框架的搭建，用户心情不错。",
            "atoms": [
                {
                    "content": "用户完成了测试框架搭建",
                    "type": "episodic",
                    "importance": 0.8,
                    "entities": ["测试框架"],
                    "confidence": 0.9,
                    "diary_snippet": "完成测试框架搭建",
                },
                {
                    "content": "用户心情不错",
                    "type": "preference",
                    "importance": 0.6,
                    "entities": [],
                    "confidence": 0.7,
                    "diary_snippet": "用户心情不错",
                },
            ],
        }),
    })


# ═══════════════════════════════════════════════════════════════
#  模拟 Store 层
# ═══════════════════════════════════════════════════════════════

class MockAtomStore:
    """AtomStore 的 AsyncMock 包装，跟踪所有调用"""

    def __init__(self):
        self.atoms: dict[int, MemoryAtom] = {}
        self._next_id = 1
        self.call_log: list[str] = []

        # 所有 async 方法都是 AsyncMock
        self.initialize = AsyncMock()
        self.search_fts = AsyncMock(return_value=[])
        self.insert_many = AsyncMock(side_effect=self._mock_insert_many)
        self.fetch = AsyncMock(return_value=[])
        self.fetchone = AsyncMock(return_value=None)
        self.execute = AsyncMock()
        self.touch = AsyncMock()
        self.get_related_user_ids = AsyncMock(return_value=[])
        self.get_by_user = AsyncMock(return_value=[])
        self.get_all_active_user_ids = AsyncMock(return_value=[])
        self._row_to_atom = MagicMock()

    async def _mock_insert_many(self, atoms: list[MemoryAtom]) -> list[int]:
        ids = []
        for atom in atoms:
            aid = self._next_id
            self._next_id += 1
            atom.atom_id = aid
            self.atoms[aid] = atom
            ids.append(aid)
        self.call_log.append(f"insert_many({len(atoms)} atoms)")
        return ids

    def clear(self):
        self.atoms.clear()
        self._next_id = 1
        self.call_log.clear()
        for attr in dir(self):
            m = getattr(self, attr, None)
            if isinstance(m, AsyncMock):
                m.reset_mock(return_value=True)


class MockDiaryStore:
    """DiaryStore 的 AsyncMock 包装"""

    def __init__(self):
        self.entries: list[dict] = []
        self._next_id = 1
        self.call_log: list[str] = []

        self.initialize = AsyncMock()
        self.append = AsyncMock(side_effect=self._mock_append)
        self.update_metadata = AsyncMock()
        self.search_fts = AsyncMock(return_value=[])

    async def _mock_append(self, user_id: str, date: str, content: str) -> int:
        did = self._next_id
        self._next_id += 1
        self.entries.append({"id": did, "user_id": user_id, "date": date, "content": content})
        self.call_log.append(f"append(diary_id={did})")
        return did


class MockGraphStore:
    """GraphStore 的 AsyncMock 包装"""

    def __init__(self):
        self.initialize = AsyncMock()
        self.upsert_nodes = AsyncMock(return_value={})
        self.add_edge = AsyncMock()
        self.add_edge_by_ids = AsyncMock()
        self.fetch = AsyncMock(return_value=[])
        self.fetchone = AsyncMock(return_value=None)
        self._now_iso = MagicMock(return_value="2026-06-11T00:00:00")


class MockWriteOpLog:
    """WriteOpLog 的 AsyncMock 包装"""

    def __init__(self):
        self.begin = AsyncMock(return_value="op_1")
        self.step = AsyncMock()
        self.complete = AsyncMock()


# ═══════════════════════════════════════════════════════════════
#  组合 Fixtures
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
def mock_atom_store():
    return MockAtomStore()


@pytest.fixture
def mock_diary_store():
    return MockDiaryStore()


@pytest.fixture
def mock_graph_store():
    return MockGraphStore()


@pytest.fixture
def mock_write_op_log():
    return MockWriteOpLog()


@pytest.fixture
def mock_memory_uow(mock_atom_store, mock_diary_store, mock_write_op_log):
    """基于 mock store 构建的 MemoryUnitOfWork"""
    from memori.storage.diary_store import DiaryStore
    from memori.storage.atom_store import AtomStore
    from memori.storage.write_op_log import WriteOpLog

    # 用 Mock 对象替换真实 store（满足类型标注）
    diary_store = MagicMock(spec=DiaryStore)
    diary_store.append = mock_diary_store.append
    diary_store.update_metadata = mock_diary_store.update_metadata

    atom_store = MagicMock(spec=AtomStore)
    atom_store.fetchone = mock_atom_store.fetchone
    atom_store.fetch = mock_atom_store.fetch
    atom_store.execute = mock_atom_store.execute
    atom_store.search_fts = mock_atom_store.search_fts
    atom_store.insert_many = mock_atom_store.insert_many
    atom_store._row_to_atom = MagicMock()

    log = MagicMock()
    log.begin = mock_write_op_log.begin
    log.step = mock_write_op_log.step
    log.complete = mock_write_op_log.complete

    return MemoryUnitOfWork(
        diary_store=diary_store,
        atom_store=atom_store,
        write_op_log=log,
    )


@pytest.fixture
def sample_capture_judge():
    return CaptureJudgeResult(
        should_remember=True,
        reason="用户分享了重要进展",
        importance=0.8,
        mood="happy",
        context_summary="用户说：我完成了测试框架的搭建",
    )


# ═══════════════════════════════════════════════════════════════
#  示例数据
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
def sample_atom():
    return MemoryAtom(
        user_id="test_user",
        diary_date="2026-06-11",
        content="用户完成了测试框架搭建",
        atom_type=AtomType.EPISODIC,
        entities=["测试框架"],
        importance=0.8,
        confidence=0.9,
        diary_snippet="完成测试框架搭建",
    )


@pytest.fixture
def sample_atoms():
    return [
        MemoryAtom(
            user_id="test_user",
            diary_date="2026-06-11",
            content="用户完成了测试框架搭建",
            atom_type=AtomType.EPISODIC,
            entities=["测试框架"],
            importance=0.8,
            confidence=0.9,
        ),
        MemoryAtom(
            user_id="test_user",
            diary_date="2026-06-11",
            content="用户心情不错",
            atom_type=AtomType.PREFERENCE,
            importance=0.6,
            confidence=0.7,
        ),
        MemoryAtom(
            user_id="test_user",
            diary_date="2026-06-10",
            content="张三邀请用户周末一起打球",
            atom_type=AtomType.PLANNED,
            entities=["张三"],
            importance=0.7,
            confidence=0.85,
        ),
    ]


import json  # noqa: E402 (used in fixtures above)
