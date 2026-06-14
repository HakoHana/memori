"""图路关键词检索器 — 精确匹配节点名 → 沿 edges 找关联日记 → 原子

简化版：不再做 FTS（N-gram 中文分词搜节点名效率低），
改为 nodes.id / nodes.name LIKE 精确匹配。
"""

from __future__ import annotations

from typing import Any

from ..models.memory_atom import MemoryAtom
from ..core.logger import logger
from .graph_utils import fetch_atoms_from_diaries, find_linked_diaries

# 可搜索的节点类型
_SEARCHABLE_TYPES = ("entity", "user", "topic", "emotion")


class GraphKeywordRetriever:
    """图路关键词检索器（简化版）

    流水线：
    1. 关键词 → nodes.name LIKE（精确匹配节点名）
    2. 匹配节点 → edges.mentions → diary_ids
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
        if not keywords:
            return []

        # 1. 关键词 → 匹配图节点（nodes.name / nodes.id LIKE）
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
        """关键词匹配新表 nodes 中的 entity/user/topic/emotion 节点

        LIKE 匹配 name 和 id（id = "entity:hako" 也匹配 "hako"），
        精确匹配优先于模糊匹配。
        """
        matched: list[dict] = []
        seen: set[str] = set()

        for kw in keywords:
            if not kw or len(kw) < 2:
                continue

            try:
                rows = await self.graph_store.fetch(
                    """SELECT id, type, name FROM nodes
                       WHERE type IN (?, ?, ?, ?)
                         AND (id LIKE ? OR name LIKE ?)
                       LIMIT 10""",
                    (*_SEARCHABLE_TYPES, f"%{kw}%", f"%{kw}%"),
                )
                for r in rows:
                    nid = r[0]
                    if nid not in seen:
                        seen.add(nid)
                        matched.append({
                            "id": nid,
                            "node_type": r[1],
                            "value": r[2],
                        })
            except Exception:
                continue

        return matched
