"""去重引擎 — jieba 词级 Jaccard + 语义去重 + 强化"""

from __future__ import annotations

import json
import math
import time
from typing import Any

import jieba

from ..models.memory_atom import MemoryAtom, AtomStatus


class DedupEngine:
    """去重强化引擎

    双重防线：
    1. jieba 词级 Jaccard 相似度（短文本降级为字符二元组）
    2. 嵌入计算后余弦相似度（阈值 0.92）

    强化策略：
    - 步长随强化次数递减（首次 +0.05，收敛到 0.01）
    - 融合 judge 重要性评分（0.7 权重 judge + 0.3 权重原值）
    - 延长 expires_at 30%
    - 回写源日记 importance
    """

    def __init__(self, atom_store, diary_store=None, config: dict[str, Any] | None = None):
        self.atom_store = atom_store
        self.diary_store = diary_store
        self.config = config or {}
        self._default_threshold = float(self.config.get("dedup_threshold", 0.6))
        self._semantic_threshold = float(self.config.get("semantic_threshold", 0.92))

    # ── 第一道：jieba 词级 Jaccard ──────────────────────────

    async def dedup_and_reinforce(
        self,
        content: str,
        user_id: str,
        judge_importance: float = 0.5,
        new_confidence: float = 0.7,
        threshold: float | None = None,
    ) -> tuple[bool, MemoryAtom | None]:
        """核心：对新增内容执行去重 + 强化

        Args:
            content: 待检查的文本内容
            user_id: 用户 ID
            judge_importance: judge 阶段给出的重要性评分
            new_confidence: 新内容的置信度
            threshold: Jaccard 相似度阈值，默认 0.6

        Returns:
            (True, matched_atom)  — 找到重复并强化
            (False, None)         — 无匹配
        """
        threshold = threshold if threshold is not None else self._default_threshold

        try:
            # jieba 词级 token（去停用词：过滤单字词）
            jieba_words = set(
                w for w in jieba.lcut(content)
                if len(w) >= 2
            )
            use_bigram_fallback = len(jieba_words) < 1

            if use_bigram_fallback:
                chars = content.replace(" ", "")
                if len(chars) < 4:
                    return False, None
                tokens = {chars[i:i + 2] for i in range(len(chars) - 1)}
                if len(tokens) < 2:
                    return False, None
            else:
                tokens = jieba_words

            query = " OR ".join(f'"{t}"' for t in list(tokens)[:6])
            now = time.time()

            # 全局搜索：原子事实无用户边界，跨用户去重
            existing = await self.atom_store.search_fts(query, user_id=None, k=5)
            for ex in existing:
                if use_bigram_fallback:
                    ex_chars = (ex.content or "").replace(" ", "")
                    if len(ex_chars) < 4:
                        continue
                    ex_tokens = {ex_chars[i:i + 2] for i in range(len(ex_chars) - 1)}
                else:
                    ex_tokens = set(
                        w for w in jieba.lcut(ex.content or "")
                        if len(w) >= 2
                    )
                if not ex_tokens:
                    continue
                union = len(tokens | ex_tokens)
                if union == 0:
                    continue
                jaccard = len(tokens & ex_tokens) / union
                if jaccard >= threshold:
                    await self._apply_reinforcement(ex, judge_importance, new_confidence, now)
                    return True, ex
        except Exception:
            pass
        return False, None

    async def _apply_reinforcement(
        self,
        atom: MemoryAtom,
        judge_importance: float,
        new_confidence: float,
        now: float | None = None,
    ):
        """强化单个原子：步长递减 + judge blend + 延长 expires_at + 回写日记"""
        if now is None:
            now = time.time()

        # 步长递减
        step = max(0.01, 0.05 / math.log2((atom.access_count or 0) + 2))
        step_boosted = atom.importance + step
        # 融合 judge
        judge_blend = judge_importance * 0.7 + atom.importance * 0.3
        boosted = min(0.95, max(step_boosted, judge_blend))
        # 延长 expires_at
        old_expires = max(atom.expires_at, now)
        new_expires = now + (old_expires - now) * 1.3

        await self.atom_store.execute(
            "UPDATE memory_atoms SET importance=?, confidence=?, "
            "access_count=access_count+1, expires_at=? WHERE id=?",
            (boosted, max(new_confidence, atom.confidence), new_expires, atom.atom_id),
        )

        # 回写源日记（diary_entries 在 diaries.db，需用 diary_store）
        if boosted > atom.importance and atom.diary_id > 0:
            try:
                store = self.diary_store or self.atom_store
                await store.execute(
                    "UPDATE diary_entries SET importance = MAX(importance, ?) WHERE id = ?",
                    (boosted, atom.diary_id),
                )
            except Exception:
                pass

    # ── forgotten 清理：旧遗忘原子与新原子重复则彻底删除 ──────

    async def cleanup_forgotten_duplicates(
        self,
        content: str,
        diary_id: int,
        user_ids: list[str],
        threshold: float = 0.6,
    ):
        """检查内容是否与已遗忘的原子重复，重复则硬删除

        在 capture 流程中，插入新原子前调用。
        避免"遗忘后再出现同一条记忆→插入新原子→旧遗忘原子仍占空间"。
        """
        try:
            chars = content.replace(" ", "")
            if len(chars) < 4:
                return
            tokens = {chars[i:i + 2] for i in range(len(chars) - 1)}
            if len(tokens) < 2:
                return

            for uid in user_ids:
                try:
                    rows = await self.atom_store.fetch(
                        "SELECT id, content FROM memory_atoms "
                        "WHERE user_id=? AND status='forgotten' AND diary_id=?",
                        (uid, diary_id),
                    )
                    for r in rows:
                        old_c = (r[1] or "").replace(" ", "")
                        if len(old_c) < 4:
                            continue
                        old_tokens = {old_c[i:i + 2] for i in range(len(old_c) - 1)}
                        if not old_tokens:
                            continue
                        u2 = len(tokens | old_tokens)
                        if u2 == 0:
                            continue
                        if len(tokens & old_tokens) / u2 >= threshold:
                            await self.atom_store.execute(
                                "DELETE FROM memory_atoms WHERE id=?", (r[0],)
                            )
                            await self.atom_store.execute(
                                "DELETE FROM memory_atoms_fts WHERE atom_id=?", (r[0],)
                            )
                except Exception:
                    pass
        except Exception:
            pass

    # ── 第二道：语义去重（余弦相似度） ──────────────────────

    async def semantic_dedup(
        self,
        atoms: list[MemoryAtom],
        model_name: str,
        threshold: float | None = None,
    ):
        """语义去重：检查新原子是否与已有原子语义重复

        供 Capturer/Call 调用（单批新原子 vs 已有库）。
        重复特征：余弦 > threshold → 标记新原子 dormant，强化旧原子。
        """
        threshold = threshold if threshold is not None else self._semantic_threshold

        for atom in atoms:
            if atom.atom_id <= 0 or not atom.embedding:
                continue

            q_emb = atom.embedding
            q_norm = sum(x * x for x in q_emb) ** 0.5
            if q_norm < 1e-10:
                continue

            try:
                rows = await self.atom_store.fetch(
                    "SELECT id, embedding, importance FROM memory_atoms "
                    "WHERE user_id=? AND status='active' AND embedding IS NOT NULL "
                    "AND embedding_model=? AND id != ? "
                    "ORDER BY importance DESC LIMIT 500",
                    (atom.user_id, model_name, atom.atom_id),
                )
            except Exception:
                continue

            for row in rows:
                eid, e_blob, e_imp = row
                if not e_blob:
                    continue
                try:
                    stored = json.loads(e_blob.decode("utf-8"))
                except Exception:
                    continue
                if not stored or len(stored) != len(q_emb):
                    continue

                dot = sum(a * b for a, b in zip(q_emb, stored))
                n_norm = sum(x * x for x in stored) ** 0.5
                if n_norm < 1e-10:
                    continue
                sim = dot / (q_norm * n_norm)

                if sim > threshold:
                    try:
                        await self.atom_store.execute(
                            "UPDATE memory_atoms SET status=? WHERE id=?",
                            (AtomStatus.DORMANT.value, atom.atom_id),
                        )
                    except Exception:
                        pass
                    step = max(0.01, 0.05 / math.log2(max(e_imp or 0, 1) + 1))
                    try:
                        await self.atom_store.execute(
                            "UPDATE memory_atoms SET importance=MIN(0.95, importance+?), "
                            "access_count=access_count+1 WHERE id=?",
                            (step, eid),
                        )
                    except Exception:
                        pass
                    break

    async def scan_semantic_duplicates(
        self,
        threshold: float | None = None,
        max_per_user: int = 300,
    ):
        """全库扫描语义重复 — 供梦境/每日定时调用

        逐用户扫描有 embedding 的活跃原子，按重要度排序，
        依次两两比较余弦相似度，超阈值则标记后者为 dormant。

        状态机接入后，由此接口接入梦境状态机调度。
        """
        threshold = threshold if threshold is not None else self._semantic_threshold

        try:
            users = await self.atom_store.fetch(
                "SELECT DISTINCT user_id FROM memory_atoms "
                "WHERE status='active' AND embedding IS NOT NULL"
            )
        except Exception:
            return 0

        total_marked = 0
        for (uid,) in users:
            try:
                rows = await self.atom_store.fetch(
                    "SELECT id, embedding, importance FROM memory_atoms "
                    "WHERE user_id=? AND status='active' AND embedding IS NOT NULL "
                    "ORDER BY importance DESC LIMIT ?",
                    (uid, max_per_user),
                )
            except Exception:
                continue

            if len(rows) < 2:
                continue

            items = []
            for row in rows:
                eid, e_blob, e_imp = row
                if not e_blob:
                    continue
                try:
                    emb = json.loads(e_blob.decode("utf-8"))
                except Exception:
                    continue
                items.append((eid, emb, e_imp))

            for i in range(min(100, len(items))):
                eid_a, emb_a, imp_a = items[i]
                norm_a = sum(x * x for x in emb_a) ** 0.5
                if norm_a < 1e-10:
                    continue
                for j in range(i + 1, len(items)):
                    eid_b, emb_b, imp_b = items[j]
                    if len(emb_a) != len(emb_b):
                        continue
                    norm_b = sum(x * x for x in emb_b) ** 0.5
                    if norm_b < 1e-10:
                        continue
                    sim = sum(a * b for a, b in zip(emb_a, emb_b)) / (norm_a * norm_b)
                    if sim > threshold:
                        if imp_a >= imp_b:
                            target_id, winner_id = eid_b, eid_a
                        else:
                            target_id, winner_id = eid_a, eid_b
                        try:
                            await self.atom_store.execute(
                                "UPDATE memory_atoms SET status=? WHERE id=?",
                                (AtomStatus.DORMANT.value, target_id),
                            )
                            total_marked += 1
                            step = max(0.01, 0.05 / math.log2(max(max(imp_a, imp_b), 1) + 1))
                            await self.atom_store.execute(
                                "UPDATE memory_atoms SET importance=MIN(0.95, importance+?), "
                                "access_count=access_count+1 WHERE id=?",
                                (step, winner_id),
                            )
                        except Exception:
                            pass
                        break
        return total_marked
    # ── 批量增强：给多个原子调权重（供 warm_processor 提前去重返回后用） ──
    # （不额外新增，父调用直接走 dedup_and_reinforce 即可）
