"""记忆写操作单元 — Capturer 专用的存储门面

封装 DiaryStore + AtomStore + WriteOpLog，
让 Capturer 只需和一个对象交互，而非三个独立 Store。

遵循最少知识原则（LoD）：Capturer 不直接操作存储层内部细节。
"""

from __future__ import annotations

from typing import Any

from ..models.memory_atom import MemoryAtom
from ..storage.diary_store import DiaryStore
from ..storage.atom_store import AtomStore


class MemoryUnitOfWork:
    """记忆写操作单元

    Capturer 通过此门面完成所有持久化操作：
    日记写入、原子插入、事实表维护、去重强化、写操作日志。

    业务逻辑（Jaccard 相似度、步长递减等）仍留在 Capturer 中，
    此类只负责数据访问的编排。
    """

    def __init__(
        self,
        diary_store: DiaryStore,
        atom_store: AtomStore,
        write_op_log=None,
    ):
        self._diary = diary_store
        self._atom = atom_store
        self._log = write_op_log

    # ── 日记操作 ──

    async def append_diary(self, user_id: str, date: str, content: str) -> int:
        """追加一篇日记，返回 diary_id"""
        return await self._diary.append(user_id, date, content)

    async def update_diary_importance(self, user_id: str, date: str, importance: float):
        """更新日记重要度"""
        await self._diary.update_metadata(user_id, date, importance=importance)

    # ── 原子查询（供去重使用）──

    async def count_active_atoms(self, diary_id: int) -> int:
        """统计某篇日记关联的活跃原子数"""
        row = await self._atom.fetchone(
            "SELECT COUNT(*) FROM memory_atoms WHERE diary_id=? AND status='active'",
            (diary_id,),
        )
        return row[0] if row else 0

    async def search_fts(self, query: str, user_id: str, k: int = 5) -> list[MemoryAtom]:
        """FTS 全文搜索原子（供去重强化用）"""
        return await self._atom.search_fts(query, user_id, k=k)

    async def fetch_forgotten(self, user_id: str, diary_id: int) -> list:
        """查询已遗忘的原子（供清理用）"""
        return await self._atom.fetch(
            "SELECT id, content FROM memory_atoms WHERE user_id=? AND status='forgotten' AND diary_id=?",
            (user_id, diary_id),
        )

    async def fetch_atom_diary(self, atom_id: int) -> int | None:
        """查询原子的源 diary_id"""
        row = await self._atom.fetchone(
            "SELECT diary_id FROM memory_atoms WHERE id=?", (atom_id,)
        )
        return row[0] if row else None

    # ── 原子写入 ──

    async def insert_atoms(self, atoms: list[MemoryAtom]) -> list[int]:
        """批量插入原子，返回 ID 列表"""
        return await self._atom.insert_many(atoms)

    async def reinforce_atom(
        self,
        atom_id: int,
        importance: float,
        confidence: float,
        expires_at: float,
    ):
        """强化已有原子：提升重要度 + 置信度 + 延长有效期 + 回写日记"""
        await self._atom.execute(
            "UPDATE memory_atoms SET importance=?, confidence=?, "
            "access_count=access_count+1, expires_at=? WHERE id=?",
            (importance, confidence, expires_at, atom_id),
        )
        # 回写源日记重要度
        diary_id = await self.fetch_atom_diary(atom_id)
        if diary_id and diary_id > 0:
            await self._atom.execute(
                "UPDATE diary_entries SET importance = MAX(importance, ?) WHERE id = ?",
                (importance, diary_id),
            )

    async def delete_forgotten_atom(self, atom_id: int):
        """彻底删除已遗忘的原子（FTS 同步清理）"""
        await self._atom.execute("DELETE FROM memory_atoms WHERE id=?", (atom_id,))
        await self._atom.execute(
            "DELETE FROM memory_atoms_fts WHERE atom_id=?", (atom_id,)
        )

    # ── 事实表 ──

    async def ensure_fact(
        self,
        content: str,
        atom_type: str,
        importance: float,
        confidence: float,
    ) -> int:
        """确保全局事实存在，返回 fact_id"""
        return await self._atom.ensure_fact(content, atom_type, importance, confidence)

    async def link_fact(
        self,
        diary_id: int,
        fact_id: int,
        importance: float,
        snippet: str,
    ):
        """将事实关联到日记"""
        await self._atom.link_fact(diary_id, fact_id, importance, snippet)

    # ── 写操作日志 ──

    async def begin_op(self, operation: str, data: dict) -> str | None:
        """开始一次写操作日志"""
        if not self._log:
            return None
        return await self._log.begin(operation, data)

    async def step_op(self, op_id: str | None, step: str):
        """记录操作步骤"""
        if op_id and self._log:
            await self._log.step(op_id, step)

    async def complete_op(self, op_id: str | None):
        """完成操作日志"""
        if op_id and self._log:
            await self._log.complete(op_id)
