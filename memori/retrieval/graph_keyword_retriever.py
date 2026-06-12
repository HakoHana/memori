"""图路关键词检索器 — FTS5/LIKE 匹配图谱节点 → 关联原子"""

from __future__ import annotations

from typing import Any

from ..models.memory_atom import MemoryAtom
from ..core.logger import logger
from .graph_utils import fetch_atoms_from_diaries, find_linked_diaries


class GraphKeywordRetriever:
    """图路关键词检索器

    流水线：
    1. 关键词 → graph_store.search_fts()（优先 FTS5 MATCH，降级 LIKE）
    2. 匹配节点 → 沿边找关联 diary_ids
    3. fetch_atoms_from_diaries() → 原子
    """

    def __init__(self, graph_store: Any, atom_store: Any):
        self.graph_store = graph_store
        self.atom_store = atom_store

    async def retrieve(
        self,
        keywords: list[str],
        user_ids: list[str],
        k: int = 5,
    ) -> list[MemoryAtom]:
        """图路关键词检索

        Args:
            keywords: 关键词列表
            user_ids: 用户 ID 列表
            k: 返回 top N

        Returns:
            按重要度降序排列的 MemoryAtom 列表
        """
        if not keywords or not user_ids:
            return []

        # 1. 关键词 → 匹配图节点（FTS5 + LIKE 双模）
        matched_nodes = await self._match_nodes(keywords)
        if not matched_nodes:
            logger.debug(f"[GraphKeyword] 未匹配到图谱节点 keywords={keywords}")
            return []

        # 2. 沿边找关联 diary_id
        node_ids = [n["id"] for n in matched_nodes]
        diary_ids = await find_linked_diaries(self.graph_store, node_ids)

        if not diary_ids:
            logger.debug(f"[GraphKeyword] 节点{node_ids}未关联到日记")
            return []

        # 3. diary_id → 原子
        atoms = await fetch_atoms_from_diaries(
            self.atom_store, diary_ids, user_ids, k,
        )

        logger.debug(f"[GraphKeyword] 节点={node_ids} 日记={diary_ids} 命中={len(atoms)}")
        return atoms

    async def _match_nodes(self, keywords: list[str]) -> list[dict]:
        """关键词匹配 graph_nodes 中的 entity/topic 节点

        优先走 FTS5（英文/数字），降级走 LIKE（中文）。
        返回去重后的匹配节点列表。
        """
        matched: list[dict] = []
        seen: set[int] = set()

        for kw in keywords:
            if not kw or len(kw) < 2:
                continue

            # 优先 FTS5（对英文/数字有效）
            if kw.isascii():
                try:
                    rows = await self.graph_store.search_fts(kw, k=10)
                    for r in rows:
                        nid = r["id"]
                        if nid not in seen:
                            seen.add(nid)
                            matched.append(r)
                    continue
                except Exception:
                    pass

            # 降级 LIKE（中文 / FTS5 兜底）
            try:
                rows = await self.graph_store.fetch(
                    """SELECT id, node_type, value, canonical_value
                       FROM graph_nodes
                       WHERE node_type IN ('entity','topic','user','emotion')
                         AND (value LIKE ? OR canonical_value LIKE ?)
                       LIMIT 10""",
                    (f"%{kw}%", f"%{kw}%"),
                )
                for r in rows:
                    nid = r[0]
                    if nid not in seen:
                        seen.add(nid)
                        matched.append({
                            "id": nid,
                            "node_type": r[1],
                            "value": r[2],
                            "canonical": r[3],
                        })
            except Exception:
                continue

        return matched
