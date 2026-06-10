"""RRF 融合 — 多路检索结果排序融合"""

from typing import Any

from ..models.memory_atom import MemoryAtom


def rrf_merge(
    ranked_lists: list[list[MemoryAtom]],
    k: int = 60,
    top_k: int = 5,
) -> list[MemoryAtom]:
    """RRF (Reciprocal Rank Fusion) 融合多路检索结果

    Args:
        ranked_lists: 多路检索结果列表（每路已按得分降序排列）
        k: RRF 常数（越大越平滑，默认 60）
        top_k: 返回 top N

    Returns:
        融合排序后的原子列表
    """
    scores: dict[int, float] = {}
    atoms: dict[int, MemoryAtom] = {}

    for ranked_list in ranked_lists:
        for rank, atom in enumerate(ranked_list):
            aid = atom.atom_id
            scores[aid] = scores.get(aid, 0.0) + 1.0 / (k + rank + 1)
            atoms[aid] = atom

    sorted_ids = sorted(scores, key=lambda x: -scores[x])
    return [atoms[aid] for aid in sorted_ids[:top_k]]
