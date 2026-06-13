"""图路检索共享工具（新表 edges 兼容版）"""

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
    """从日记 ID 取关联原子（通过 atoms_diary_links 桥表）"""
    if not diary_ids:
        return []

    did_placeholders = ",".join("?" for _ in diary_ids)
    uid_placeholders = ",".join("?" for _ in user_ids)

    order_by = "d.importance DESC" if score_multiplier == 1.0 else f"({score_multiplier} * d.importance) DESC"
    try:
        atom_rows = await atom_store.fetch(
            f"""SELECT a.* FROM memory_atoms a
                JOIN atoms_diary_links d ON a.id = d.atom_id
                WHERE d.diary_id IN ({did_placeholders})
                  AND a.user_id IN ({uid_placeholders})
                  AND a.status = 'active'
                ORDER BY {order_by}
                LIMIT ?""",
            (*diary_ids, *user_ids, k),
        )
        if atom_rows:
            return [atom_store._row_to_atom(r) for r in atom_rows]
    except Exception:
        pass

    return []


async def find_linked_diaries(graph_store: Any, node_ids: list[str]) -> list[int]:
    """从节点 ID 沿 edges 表 mention 边找到关联的 diary_id

    Args:
        graph_store: GraphStore 实例
        node_ids: 节点 TEXT ID 列表（如 ["entity:hako", "entity:coffee"]）

    Returns:
        关联的 diary_id 列表
    """
    if not node_ids:
        return []

    placeholders = ",".join("?" for _ in node_ids)
    try:
        rows = await graph_store.fetch(
            f"""SELECT DISTINCT diary_id
                FROM edges
                WHERE from_node IN ({placeholders})
                  AND relation_type = 'mentions'
                  AND diary_id > 0
                ORDER BY diary_id DESC
                LIMIT 30""",
            node_ids,
        )
        return [r[0] for r in rows if r[0] > 0]
    except Exception:
        return []


