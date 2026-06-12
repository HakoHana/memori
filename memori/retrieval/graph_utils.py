"""图路检索共享工具 — fetch_atoms_from_diaries + 辅助函数"""

from __future__ import annotations

from typing import Any

from ..models.memory_atom import MemoryAtom, AtomType


async def fetch_atoms_from_diaries(
    atom_store: Any,
    diary_ids: list[int],
    user_ids: list[str],
    k: int,
    score_multiplier: float = 1.0,
) -> list[MemoryAtom]:
    """从日记 ID 取关联原子，支持分数加权

    优先从 atomic_facts 取（语义化），回退到 memory_atoms（原始原子）。
    score_multiplier 在 ORDER BY 层面生效，保留原子粒度的分数差异性。

    Args:
        atom_store: AtomStore 实例
        diary_ids: 日记 ID 列表
        user_ids: 用户 ID 列表
        k: 返回 top N
        score_multiplier: 分数系数（用于 GraphVectorRetriever 传入节点相似度）

    Returns:
        按加权分数降序排列的 MemoryAtom 列表
    """
    if not diary_ids:
        return []

    did_placeholders = ",".join("?" for _ in diary_ids)
    uid_placeholders = ",".join("?" for _ in user_ids)

    # 当 score_multiplier != 1.0 时，ORDER BY 乘以该系数
    order_expr = f"({score_multiplier} * af.importance) DESC" if score_multiplier != 1.0 else "af.importance DESC"

    # 路径 A：通过 diary_fact_links → atomic_facts
    try:
        fact_rows = await atom_store.fetch(
            f"""SELECT DISTINCT af.id, af.content, af.atom_type,
                       af.importance, af.confidence, af.created_at
                FROM atomic_facts af
                JOIN diary_fact_links dfl ON af.id = dfl.fact_id
                WHERE dfl.diary_id IN ({did_placeholders})
                ORDER BY {order_expr}
                LIMIT ?""",
            (*diary_ids, k * 2),
        )
        if fact_rows:
            return [_fact_row_to_atom(r, user_ids[0]) for r in fact_rows[:k]]
    except Exception:
        pass

    # 路径 B：回退到 memory_atoms
    order_expr2 = f"({score_multiplier} * importance) DESC" if score_multiplier != 1.0 else "importance DESC"
    try:
        atom_rows = await atom_store.fetch(
            f"""SELECT * FROM memory_atoms
                WHERE diary_id IN ({did_placeholders})
                  AND user_id IN ({uid_placeholders})
                  AND status = 'active'
                ORDER BY {order_expr2}
                LIMIT ?""",
            (*diary_ids, *user_ids, k),
        )
        if atom_rows:
            return [atom_store._row_to_atom(r) for r in atom_rows]
    except Exception:
        pass

    return []


async def find_linked_diaries(graph_store: Any, node_ids: list[int]) -> list[int]:
    """从节点 ID 沿边找到关联的 diary_id

    匹配两种边：
    - mentions: entity → diary
    - has_meta: topic/emotion/date → diary
    """
    if not node_ids:
        return []

    placeholders = ",".join("?" for _ in node_ids)
    try:
        rows = await graph_store.fetch(
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


def _fact_row_to_atom(row, user_id: str) -> MemoryAtom:
    """将 atomic_facts 行包装为 MemoryAtom（兼容下游接口）"""
    return MemoryAtom(
        atom_id=row[0],
        user_id=user_id,
        diary_date="",
        content=row[1],
        atom_type=AtomType(row[2]) if row[2] else AtomType.factual,
        importance=float(row[3]) if row[3] else 0.5,
        confidence=float(row[4]) if row[4] else 0.8,
        created_at=float(row[5]) if row[5] else 0.0,
    )
