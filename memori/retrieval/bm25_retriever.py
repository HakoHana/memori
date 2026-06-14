"""文档路：FTS5（英文）+ LIKE（中文）双模检索"""

from __future__ import annotations

import re
from typing import Any

from ..models.memory_atom import MemoryAtom
from ..storage.atom_store import AtomStore
from ..core.logger import logger


class BM25Retriever:
    """文档路检索器

    FTS5 unicode61 不切分连续汉字（"宠物狗"是一个 token），
    所以中文关键词走 LIKE %kw%，英文/数字走 FTS5 MATCH。

    两路结果按重要度融合排序。
    """

    def __init__(self, atom_store: AtomStore):
        self.atom_store = atom_store

    async def retrieve(
        self,
        keywords: list[str],
        user_ids: list[str],
        k: int = 5,
    ) -> list[MemoryAtom]:
        """混合检索

        Args:
            keywords: 关键词列表（已分词去停用词）
            user_ids: 用户 ID 列表，为空 = 全库搜索（不限 user_id）
            k: 返回 top N

        Returns:
            按重要度降序排列的 MemoryAtom 列表
        """
        if not keywords:
            return []

        # 分拣：英文/数字 → FTS5，中文 → LIKE
        ascii_kws = [kw for kw in keywords if kw.isascii() and len(kw) >= 2]
        cjk_kws = [kw for kw in keywords if not kw.isascii() and len(kw) >= 2]

        results: dict[int, MemoryAtom] = {}
        scores: dict[int, float] = {}

        # ── 英文/数字路：FTS5 ──
        if ascii_kws:
            try:
                fts_query = " OR ".join(ascii_kws)
                if user_ids:
                    uid = user_ids[0]
                    extra = user_ids[1:] if len(user_ids) > 1 else None
                    fts_atoms = await self.atom_store.search_fts(
                        query=fts_query, user_id=uid, k=k, extra_user_ids=extra,
                    )
                else:
                    fts_atoms = await self.atom_store.search_fts(
                        query=fts_query, user_id=None, k=k,
                    )
                for a in fts_atoms:
                    results[a.atom_id] = a
                    scores[a.atom_id] = a.importance
            except Exception as e:
                logger.debug(f"[BM25] FTS5 检索异常: {e}")

        # ── 中文路：LIKE %kw% ──
        if cjk_kws:
            try:
                for kw in cjk_kws:
                    if user_ids:
                        for uid in user_ids:
                            rows = await self.atom_store.fetch(
                                """SELECT * FROM memory_atoms
                                   WHERE user_id=? AND status='active'
                                     AND content LIKE ?
                                   ORDER BY importance DESC LIMIT ?""",
                                (uid, f"%{kw}%", k),
                            )
                            for r in rows:
                                atom = self.atom_store._row_to_atom(r)
                                results[atom.atom_id] = atom
                                scores[atom.atom_id] = scores.get(atom.atom_id, 0.0) + 0.3
                    else:
                        rows = await self.atom_store.fetch(
                            """SELECT * FROM memory_atoms
                               WHERE status='active'
                                 AND content LIKE ?
                               ORDER BY importance DESC LIMIT ?""",
                            (f"%{kw}%", k),
                        )
                        for r in rows:
                            atom = self.atom_store._row_to_atom(r)
                            results[atom.atom_id] = atom
                            scores[atom.atom_id] = scores.get(atom.atom_id, 0.0) + 0.3
            except Exception as e:
                logger.debug(f"[BM25] LIKE 检索异常: {e}")

        if not results:
            return []

        # 综合排序：重要度 × 0.7 + 关键词命中数 × 0.3
        sorted_atoms = sorted(
            results.values(),
            key=lambda a: (a.importance * 0.7 + min(scores.get(a.atom_id, 0), 1.0) * 0.3, a.importance),
            reverse=True,
        )
        return sorted_atoms[:k]
