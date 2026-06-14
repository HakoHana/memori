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
    日记写入、原子插入、写操作日志。

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

    async def append_diary(self, date: str, content: str) -> int:
        """追加一篇日记，返回 diary_id"""
        return await self._diary.append(date, content)

    async def update_diary_importance(
        self, date: str, importance: float, diary_id: int = 0,
    ):
        """更新日记重要度

        优先按 diary_id 精确匹配，否则回退到 date 最新条目。
        """
        if diary_id > 0:
            await self._diary.update_metadata_by_id(diary_id, importance=importance)
        else:
            await self._diary.update_metadata(date, importance=importance)

    # ── 原子查询（供去重使用）──

    async def search_fts(self, query: str, user_id: str, k: int = 5) -> list[MemoryAtom]:
        """FTS 全文搜索原子（供去重强化用）"""
        return await self._atom.search_fts(query, user_id, k=k)

    # ── 原子写入 ──

    @property
    def atom_store(self):
        """暴露原子存储（供 Capturer 调桥表方法）"""
        return self._atom

    async def insert_atoms(self, atoms: list[MemoryAtom]) -> list[int]:
        """批量插入原子，返回 ID 列表"""
        return await self._atom.insert_many(atoms)

    async def delete_forgotten_atom(self, atom_id: int):
        """彻底删除已遗忘的原子（FTS 同步清理）"""
        await self._atom.execute("DELETE FROM memory_atoms WHERE id=?", (atom_id,))
        await self._atom.execute(
            "DELETE FROM memory_atoms_fts WHERE atom_id=?", (atom_id,)
        )

    # ── 向量 embedding ──

    async def update_embedding(self, atom_id: int, embedding: list[float], model_name: str):
        """写入单条原子的 embedding 向量"""
        await self._atom.update_embedding(atom_id, embedding, model_name)

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
