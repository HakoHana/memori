"""身份相似检测引擎 — 对比不同 canonical uid 的画像嵌入，标记潜在同一人

每日定时扫描 + 留接口供未来梦境状态机使用。

流程：
1. 从 user_persona 取出所有有 embedding 的用户
2. pairwise cosine similarity 计算
3. 超过阈值 → 更新 canonical_users.identity_confidence
4. 不自动合并，只标记，等用户亲口确认或梦境裁决
"""

from __future__ import annotations

import math
from typing import Any

from ..core.logger import logger

# 相似度阈值：超过此值标记为"可能是同一人"
DEFAULT_SIMILARITY_THRESHOLD = 0.85


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """余弦相似度"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class IdentityEngine:
    """身份相似检测引擎

    使用方式（由 LifecycleManager.run_daily_identity_check 调用）：
        engine = IdentityEngine(atom_store)
        matches = await engine.scan_similar_personas(threshold=0.85)
        for m in matches:
            logger.info(f"潜在同一人: {m['uid_a']} <-> {m['uid_b']} ({m['similarity']:.2f})")
    """

    def __init__(self, atom_store, config: dict[str, Any] | None = None):
        self._atom_store = atom_store
        self._config = config or {}

    async def scan_similar_personas(
        self, threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    ) -> list[dict]:
        """全库扫描：对比所有用户画像嵌入，返回相似度超过阈值的配对

        Args:
            threshold: 余弦相似度阈值（默认 0.85）

        Returns:
            [{"uid_a", "name_a", "uid_b", "name_b", "similarity"}, ...]
            按相似度降序排列
        """
        personas = await self._atom_store.get_all_persona_embeddings()
        if len(personas) < 2:
            return []

        matches = []
        compared = set()

        for i in range(len(personas)):
            for j in range(i + 1, len(personas)):
                key = frozenset([personas[i]["uid"], personas[j]["uid"]])
                if key in compared:
                    continue
                compared.add(key)

                a, b = personas[i], personas[j]
                sim = cosine_similarity(a["embedding"], b["embedding"])
                if sim >= threshold:
                    matches.append({
                        "uid_a": a["uid"],
                        "name_a": a["primary_name"],
                        "uid_b": b["uid"],
                        "name_b": b["primary_name"],
                        "similarity": round(sim, 4),
                    })

        matches.sort(key=lambda x: -x["similarity"])
        return matches

    async def mark_similar_pair(
        self, uid_a: str, uid_b: str, similarity: float,
    ) -> None:
        """标记两个 uid 为潜在同一人（更新 identity_confidence）"""
        now = __import__("time").time()
        confidence = min(0.95, 0.3 + similarity * 0.7)
        for uid in (uid_a, uid_b):
            await self._atom_store.execute(
                "UPDATE canonical_users SET identity_confidence=?, updated_at=? WHERE uid=?",
                (confidence, now, uid),
            )
        logger.info(
            f"[Identity] 标记潜在同一人: {uid_a} <-> {uid_b} "
            f"(相似度={similarity:.2f}, confidence={confidence:.2f})"
        )

    async def merge_identities(self, keep_uid: str, merge_uid: str) -> bool:
        """梦境/手动确认后合并身份：将 merge_uid 的 identities 全部转给 keep_uid

        这是未来梦境状态机调用的接口。
        合并后 merge_uid 不再使用，但保留记录。
        """
        try:
            now = __import__("time").time()
            # 转移所有 platform_id 到 keep_uid
            await self._atom_store.execute(
                "UPDATE user_identities SET uid=?, verified=1, last_seen=? WHERE uid=?",
                (keep_uid, now, merge_uid),
            )
            # 合并 persona（保留 keep_uid 的，追加 merge_uid 的标签）
            keep_p = await self._atom_store.get_user_persona(keep_uid)
            merge_p = await self._atom_store.get_user_persona(merge_uid)
            if keep_p and merge_p:
                import json
                keep_tags = set(json.loads(keep_p.get("tags", "[]")) if isinstance(keep_p.get("tags"), str) else keep_p.get("tags", []))
                merge_tags = set(json.loads(merge_p.get("tags", "[]")) if isinstance(merge_p.get("tags"), str) else merge_p.get("tags", []))
                merged = keep_tags | merge_tags
                await self._atom_store.execute(
                    "UPDATE user_persona SET tags=?, known_ids=?, updated_at=? WHERE uid=?",
                    (json.dumps(list(merged), ensure_ascii=False),
                     json.dumps([keep_uid, merge_uid], ensure_ascii=False), now, keep_uid),
                )
            # 标记 merge_uid 已合并
            await self._atom_store.execute(
                "UPDATE canonical_users SET primary_name=?, identity_confidence=?, updated_at=? WHERE uid=?",
                (f"已合并至{keep_uid}", 1.0, now, merge_uid),
            )
            logger.info(f"[Identity] 身份合并完成: {merge_uid} → {keep_uid}")
            return True
        except Exception as e:
            logger.error(f"[Identity] 身份合并失败 {merge_uid}→{keep_uid}: {e}")
            return False
