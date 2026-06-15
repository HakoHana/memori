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

    三道防线：
    1. jieba 词级 Jaccard 相似度（短文本降级为字符二元组） — 写入路径
    2. Jieba → FTS 粗召回 → Jaccard 筛选 → 余弦精排      — 写入路径语义去重
    3. Jieba → FTS 粗召回 → Jaccard 筛选 → 余弦精排      — 梦境定时扫描（低阈值兜底）

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

    # ── 工具：Jieba 分词（短文本降级为二元组） ────────────

    @staticmethod
    def _tokenize(text: str) -> set[str] | None:
        """Tokenize 文本 → jieba 词集，短文本降级为字符二元组

        Returns None 表示无法 tokenize（空或过短）
        """
        if not text:
            return None
        words = set(w for w in jieba.lcut(text) if len(w) >= 2)
        if words:
            return words
        # 短文本降级：字符二元组
        chars = text.replace(" ", "")
        if len(chars) < 4:
            return None
        bigrams = {chars[i:i + 2] for i in range(len(chars) - 1)}
        return bigrams if len(bigrams) >= 2 else None

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
            tokens = self._tokenize(content)
            if not tokens:
                return False, None

            query = " OR ".join(f'"{t}"' for t in list(tokens)[:6])
            now = time.time()

            # 全局搜索：原子事实无用户边界，跨用户去重
            existing = await self.atom_store.search_fts(query, user_id=None, k=5, rank_only=True)
            for ex in existing:
                ex_tokens = self._tokenize(ex.content or "")
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

        # 回写源日记重要度 — 旧列 + 桥表双路径
        if boosted > atom.importance:
            diary_ids: list[int] = []
            if atom.diary_id > 0:
                diary_ids.append(atom.diary_id)
            else:
                # 新原子通过桥表查多对多关联
                try:
                    links = await self.atom_store.fetch(
                        "SELECT diary_id FROM atoms_diary_links WHERE atom_id=?",
                        (atom.atom_id,),
                    )
                    diary_ids = [r[0] for r in links if r[0] > 0]
                except Exception:
                    pass
            for did in diary_ids:
                try:
                    store = self.diary_store or self.atom_store
                    await store.execute(
                        "UPDATE diary_entries SET importance = MAX(importance, ?) WHERE id = ?",
                        (boosted, did),
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
                        "WHERE user_id=? AND status='forgotten'",
                        (uid,),
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

    # ── 第二道：写入路径语义去重（新原子 vs 库里已有原子） ───

    async def semantic_dedup_new_atoms(
        self,
        atoms: list[MemoryAtom],
        diary_id: int,
        model_name: str,
        threshold: float = 0.90,
        jaccard_threshold: float = 0.3,
    ) -> list[MemoryAtom]:
        """写入路径语义去重：新原子（已有内存 embedding）vs 库里已有原子

        Jieba → FTS 粗召回 → Jaccard 筛选候选 → 余弦精排

        调用前：新原子必须已有 in-memory embedding（atom.embedding 非空）
        调用后：重复原子已链接日记到旧原子，不会出现在返回值中

        Args:
            atoms: 已有内存 embedding 的新原子列表
            diary_id: 当前日记 ID（用于链接）
            model_name: embedding 模型名
            threshold: 余弦相似度阈值（默认 0.90）
            jaccard_threshold: Jaccard 粗筛阈值（默认 0.3）

        Returns:
            真正不重复的新原子列表（可安全入库）
        """
        if not atoms or not self.atom_store:
            return atoms

        atom_store = self.atom_store
        filtered = []

        for atom in atoms:
            if not atom.embedding or not atom.content:
                filtered.append(atom)
                continue

            q_emb = atom.embedding
            q_norm = math.sqrt(sum(x * x for x in q_emb))
            if q_norm < 1e-10:
                filtered.append(atom)
                continue

            # 1. Jieba → FTS 粗召回（同用户，活跃）
            tokens = self._tokenize(atom.content)
            if not tokens:
                filtered.append(atom)
                continue

            query = " OR ".join(f'"{t}"' for t in list(tokens)[:6])
            try:
                candidates = await atom_store.search_fts(
                    query, user_id=atom.user_id, rank_only=True,
                )
            except Exception:
                filtered.append(atom)
                continue

            # 2. Jaccard 粗筛 → 余弦精排
            found = False
            for cand in candidates:
                if cand.atom_id == atom.atom_id or not cand.embedding:
                    continue

                # Jaccard 粗筛
                cand_tokens = self._tokenize(cand.content or "")
                if not cand_tokens:
                    continue
                union = len(tokens | cand_tokens)
                if union == 0:
                    continue
                jaccard = len(tokens & cand_tokens) / union
                if jaccard < jaccard_threshold:
                    continue

                # 余弦精排
                c_emb = cand.embedding
                if len(c_emb) != len(q_emb):
                    continue
                c_norm = math.sqrt(sum(x * x for x in c_emb))
                if c_norm < 1e-10:
                    continue
                sim = sum(a * b for a, b in zip(q_emb, c_emb)) / (q_norm * c_norm)

                if sim > threshold:
                    # 重复 → 强化旧原子，链接日记，不插入新的
                    await self._apply_reinforcement(
                        cand, judge_importance=atom.importance,
                        new_confidence=atom.confidence,
                    )
                    if diary_id > 0:
                        try:
                            await atom_store.link_atom_to_diary(
                                cand.atom_id, diary_id,
                                snippet=atom.diary_snippet,
                                importance=atom.importance,
                            )
                        except Exception:
                            pass
                    found = True
                    break

            if not found:
                filtered.append(atom)

        return filtered

    # ── 第三道：梦境定时语义去重（全库扫描，低阈值兜底） ────

    async def scan_semantic_duplicates(
        self,
        threshold: float = 0.88,
        jaccard_threshold: float = 0.3,
    ):
        """全库扫描语义重复 — 供梦境每日定时调用

        Jieba → FTS 粗召回 → Jaccard 筛选候选 → 余弦精排

        相比旧版本（O(n²) + range 100 bug）：
        - FTS 定位候选，避免全量两两比较
        - 不做范围截断，不遗漏低重要度原子

        Args:
            threshold: 余弦相似度阈值（默认 0.88，比写入时松）
            jaccard_threshold: Jaccard 粗筛阈值（默认 0.3）

        Returns:
            标记为 dormant 的原子数量
        """
        try:
            users = await self.atom_store.fetch(
                "SELECT DISTINCT user_id FROM memory_atoms "
                "WHERE status='active' AND embedding IS NOT NULL"
            )
        except Exception:
            return 0

        total_marked = 0

        for (uid,) in users:
            # 加载该用户所有有 embedding 的活跃原子（全量，不截断）
            try:
                rows = await self.atom_store.fetch("""
                    SELECT id, content, embedding, importance FROM memory_atoms
                    WHERE user_id=? AND status='active' AND embedding IS NOT NULL
                    ORDER BY importance DESC
                """, (uid,))
            except Exception:
                continue

            if len(rows) < 2:
                continue

            # 构建查找索引
            id_to_info: dict[int, dict] = {}
            id_order: list[int] = []
            for row in rows:
                eid, content, e_blob, imp = row
                if not e_blob:
                    continue
                try:
                    emb = json.loads(e_blob.decode("utf-8"))
                except Exception:
                    continue
                if not emb:
                    continue
                norm = math.sqrt(sum(x * x for x in emb))
                if norm < 1e-10:
                    continue
                tokens = self._tokenize(content or "")
                id_to_info[eid] = {
                    "emb": emb, "norm": norm, "imp": imp,
                    "tokens": tokens,
                }
                id_order.append(eid)

            if len(id_order) < 2:
                continue

            marked: set[int] = set()

            for eid_a in id_order:
                if eid_a in marked:
                    continue

                info_a = id_to_info[eid_a]
                tokens_a = info_a["tokens"]
                if not tokens_a:
                    continue

                # FTS 粗召回
                query = " OR ".join(f'"{t}"' for t in list(tokens_a)[:6])
                try:
                    candidates = await self.atom_store.search_fts(
                        query, user_id=uid, rank_only=True,
                    )
                except Exception:
                    continue

                for cand in candidates:
                    eid_b = cand.atom_id
                    if eid_b in marked or eid_b == eid_a:
                        continue
                    if eid_b not in id_to_info:
                        continue

                    info_b = id_to_info[eid_b]

                    # Jaccard 粗筛
                    tokens_b = info_b["tokens"]
                    if not tokens_b:
                        continue
                    union = len(tokens_a | tokens_b)
                    if union == 0:
                        continue
                    jaccard = len(tokens_a & tokens_b) / union
                    if jaccard < jaccard_threshold:
                        continue

                    # 余弦精排
                    if len(info_a["emb"]) != len(info_b["emb"]):
                        continue
                    sim = sum(a * b for a, b in zip(info_a["emb"], info_b["emb"]))
                    sim /= info_a["norm"] * info_b["norm"]

                    if sim > threshold:
                        imp_a, imp_b = info_a["imp"], info_b["imp"]
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
                            marked.add(target_id)
                            step = max(0.01, 0.05 / math.log2(max(max(imp_a, imp_b), 1) + 1))
                            await self.atom_store.execute(
                                "UPDATE memory_atoms SET importance=MIN(0.95, importance+?), "
                                "access_count=access_count+1 WHERE id=?",
                                (step, winner_id),
                            )
                        except Exception:
                            pass
                        break  # 找到一条重复就退出内循环

        return total_marked
    # ── 批量增强：给多个原子调权重（供 warm_processor 提前去重返回后用） ──
    # （不额外新增，父调用直接走 dedup_and_reinforce 即可）
