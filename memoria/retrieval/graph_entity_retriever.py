"""图路：实体 → 图谱 → 相关原子"""

from __future__ import annotations

from typing import Any

from ..models.memory_atom import MemoryAtom
from ..storage.graph_store import GraphStore
from ..storage.atom_store import AtomStore
from ..core.logger import logger


class GraphEntityRetriever:
    """图路检索器

    流水线：
    1. 关键词 → 匹配 graph_nodes 中的 entity/topic 节点（LIKE value）
    2. 节点 → graph_edges(mentions/has_meta) → 关联的 diary_id
    3. diary_id → atomic_facts 或 memory_atoms（按重要度取 top）
    """

    def __init__(self, graph_store: GraphStore, atom_store: AtomStore):
        self.graph_store = graph_store
        self.atom_store = atom_store
        self._min_diaries_for_fallback = 3  # 图路未命中时回退使用

    async def retrieve(
        self,
        keywords: list[str],
        user_ids: list[str],
        k: int = 5,
    ) -> list[MemoryAtom]:
        """图路检索

        先查 graph_nodes 匹配实体/主题,
        再沿边找到关联日记,
        最后取出日记下的原子/事实。
        """
        if not keywords or not user_ids:
            return []

        # 1. 关键词 → 匹配实体/主题节点
        matched_nodes = await self._match_nodes(keywords)
        if not matched_nodes:
            logger.debug(f"[GraphEntity] 未匹配到图谱节点 keywords={keywords}")
            return []

        # 2. 沿 mentions/has_meta 边找关联 diary_id
        node_ids = [n["id"] for n in matched_nodes]
        diary_ids = await self._find_linked_diaries(node_ids)

        if not diary_ids:
            logger.debug(f"[GraphEntity] 节点{node_ids}未关联到日记")
            return []

        # 3. diary_id → atomic_facts / memory_atoms
        atoms = await self._fetch_atoms_from_diaries(diary_ids, user_ids, k)

        logger.debug(f"[GraphEntity] 节点={node_ids} 日记={diary_ids} 命中={len(atoms)}")
        return atoms

    async def _match_nodes(self, keywords: list[str]) -> list[dict]:
        """关键词匹配 graph_nodes 中的 entity/topic/emotion 节点"""
        matched = []
        for kw in keywords:
            if not kw or len(kw) < 2:
                continue
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
                    matched.append({
                        "id": r[0],
                        "type": r[1],
                        "value": r[2],
                        "canonical": r[3],
                    })
            except Exception:
                continue

        # 去重（按 node_id）
        seen: set[int] = set()
        deduped = []
        for n in matched:
            if n["id"] not in seen:
                seen.add(n["id"])
                deduped.append(n)
        return deduped

    async def _find_linked_diaries(self, node_ids: list[int]) -> list[int]:
        """从节点ID沿边找到关联的 diary_id

        匹配两种边：
        - mentions: entity → diary
        - has_meta: topic/emotion/date → diary
        """
        if not node_ids:
            return []

        placeholders = ",".join("?" for _ in node_ids)
        # 注意：graph_edges.source_memory_id 即 diary_id
        try:
            rows = await self.graph_store.fetch(
                f"""SELECT DISTINCT e.source_memory_id
                    FROM graph_edges e
                    WHERE e.source_node_id IN ({placeholders})
                      AND e.relation_type IN ('mentions', 'has_meta')
                      AND e.source_memory_id > 0
                    ORDER BY e.source_memory_id DESC
                    LIMIT 30""",
                node_ids,
            )
            return [r[0] for r in rows if r[0] > 0]
        except Exception:
            return []

    async def _fetch_atoms_from_diaries(
        self, diary_ids: list[int], user_ids: list[str], k: int
    ) -> list[MemoryAtom]:
        """从日记 ID 取关联原子

        优先从 atomic_facts 取（语义化），
        回退到 memory_atoms（原始原子）。
        """
        if not diary_ids:
            return []

        did_placeholders = ",".join("?" for _ in diary_ids)
        uid_placeholders = ",".join("?" for _ in user_ids)

        # 路径 A：通过 diary_fact_links → atomic_facts
        try:
            fact_rows = await self.atom_store.fetch(
                f"""SELECT DISTINCT af.id, af.content, af.atom_type,
                           af.importance, af.confidence, af.created_at
                    FROM atomic_facts af
                    JOIN diary_fact_links dfl ON af.id = dfl.fact_id
                    WHERE dfl.diary_id IN ({did_placeholders})
                    ORDER BY af.importance DESC
                    LIMIT ?""",
                (*diary_ids, k * 2),
            )
            if fact_rows:
                # 转成 MemoryAtom 风格
                return [self._fact_row_to_atom(r, user_ids[0]) for r in fact_rows[:k]]
            logger.debug(f"[GraphEntity] atomic_facts 路径无结果 diary_ids={diary_ids}")
        except Exception as e:
            logger.debug(f"[GraphEntity] atomic_facts 查询异常: {e}")

        # 路径 B：回退到 memory_atoms
        try:
            atom_rows = await self.atom_store.fetch(
                f"""SELECT * FROM memory_atoms
                    WHERE diary_id IN ({did_placeholders})
                      AND user_id IN ({uid_placeholders})
                      AND status = 'active'
                    ORDER BY importance DESC
                    LIMIT ?""",
                (*diary_ids, *user_ids, k),
            )
            if atom_rows:
                return [self.atom_store._row_to_atom(r) for r in atom_rows]
        except Exception:
            pass

        return []

    @staticmethod
    def _fact_row_to_atom(row, user_id: str) -> MemoryAtom:
        """将 atomic_facts 行包装为 MemoryAtom（兼容下游接口）"""
        from ..models.memory_atom import MemoryAtom, AtomType
        return MemoryAtom(
            atom_id=row[0],
            user_id=user_id,
            diary_date="",  # atomic_facts 没有日记日期
            content=row[1],
            atom_type=AtomType(row[2]) if row[2] else AtomType.factual,
            importance=float(row[3]) if row[3] else 0.5,
            confidence=float(row[4]) if row[4] else 0.8,
            created_at=float(row[5]) if row[5] else 0.0,
        )
