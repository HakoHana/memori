"""检索引擎 — 召回相关记忆（混合：RRF 多路融合）"""

from __future__ import annotations

import re
from typing import Any

from ..models.memory_atom import MemoryAtom, RecallResult
from ..storage.atom_store import AtomStore
from ..storage.diary_store import DiaryStore
from ..storage.persona_store import PersonaStore


class Retriever:
    """
    记忆检索引擎

    双通道检索：
    - 原子事实（atomic_facts 结构化匹配）
    - 日记全文（diary_fts FTS5）
    """

    def __init__(
        self,
        atom_store: AtomStore,
        persona_store: PersonaStore,
        diary_store: DiaryStore | None = None,
        config: dict[str, Any] | None = None,
    ):
        self.atom_store = atom_store
        self.persona_store = persona_store
        self.diary_store = diary_store
        self.config = config or {}
        self.recall_count = self.config.get("recall_count", 5)
        self.recall_max_tokens = self.config.get("recall_max_tokens", 500)

    # ── RRF 融合 ──
    RRF_K = 60

    @staticmethod
    def _rrf_merge(lists: list[list[MemoryAtom]], k: int) -> list[MemoryAtom]:
        """RRF 融合多路搜索：在越多列表中排名越靠前的原子得分越高"""
        scores: dict[int, float] = {}
        atoms: dict[int, MemoryAtom] = {}
        for ranked_list in lists:
            for rank, atom in enumerate(ranked_list):
                aid = atom.atom_id
                scores[aid] = scores.get(aid, 0.0) + 1.0 / (Retriever.RRF_K + rank + 1)
                atoms[aid] = atom
        sorted_ids = sorted(scores, key=lambda x: -scores[x])
        return [atoms[aid] for aid in sorted_ids[:k]]

    @staticmethod
    def _segment(text: str) -> list[str]:
        """Unicode 正则分词：提取中英文、数字、下划线序列"""
        if not text or not text.strip():
            return []
        return re.findall(r'[\w]+', text, re.UNICODE)

    _ZH_STOP = frozenset({
        "的", "了", "在", "是", "我", "你", "他", "她", "它",
        "有", "不", "也", "就", "都", "而", "及", "与", "和",
        "这", "那", "什么", "怎么", "吗", "吧", "呢", "啊", "哦",
        "嗯", "哈", "呀", "啦", "嘛", "一个", "这个", "那个",
        "会", "能", "可以", "要", "想", "给", "把", "被",
        "好", "很", "太", "更", "最", "真", "还", "又", "再",
    })

    def _keyword_list(self, text: str) -> list[str]:
        """提取关键词：Unicode 分词 + 中文 2gram（不加单字，减少噪音）"""
        tokens = self._segment(text)
        keywords = []
        for token in tokens:
            if re.match(r'^[a-z0-9_]+$', token.lower()):
                # 纯英文/数字词作为整体
                if token.lower() not in keywords:
                    keywords.append(token.lower())
            else:
                # 中文：2-gram
                chars = list(token)
                for i in range(len(chars) - 1):
                    bg = chars[i] + chars[i+1]
                    if bg not in self._ZH_STOP and bg not in keywords:
                        keywords.append(bg)
        # 去重 + 截断
        seen = set()
        return [k for k in keywords if not (k in seen or seen.add(k))][:15]

    async def recall(self, user_id: str, query: str, k: int | None = None) -> list[MemoryAtom]:
        """召回：关键词匹配计数 + 重要度 加权排序"""
        k = k or self.recall_count
        if not query or not query.strip():
            return []

        extra_ids = await self.atom_store.get_related_user_ids(user_id)
        all_uids = list(set([user_id] + extra_ids))
        keywords = self._keyword_list(query)

        # ── 关键词计数 ──
        score: dict[int, float] = {}
        atoms: dict[int, MemoryAtom] = {}
        for kw in keywords:
            for uid in all_uids:
                try:
                    rows = await self.atom_store.fetch(
                        "SELECT * FROM memory_atoms WHERE user_id=? AND status='active' AND content LIKE ? ORDER BY importance DESC LIMIT ?",
                        (uid, f"%{kw}%", k * 4),
                    )
                    for r in rows:
                        atom = self.atom_store._row_to_atom(r)
                        aid = atom.atom_id
                        score[aid] = score.get(aid, 0.0) + 1.0  # 每匹配一个关键词 +1
                        atoms[aid] = atom
                except Exception:
                    pass

        # ── 关键词未命中时，重要度回退 ──
        if len(atoms) < k:
            for uid in all_uids:
                try:
                    rows = await self.atom_store.fetch(
                        "SELECT * FROM memory_atoms WHERE user_id=? AND status='active' ORDER BY importance DESC, id DESC LIMIT ?",
                        (uid, k * 3),
                    )
                    for r in rows:
                        atom = self.atom_store._row_to_atom(r)
                        aid = atom.atom_id
                        if aid not in atoms:
                            score[aid] = 0.0
                            atoms[aid] = atom
                except Exception:
                    pass

        # ── 综合评分排序 ──
        max_id = max(atoms.keys()) if atoms else 1
        def _sort_key(a: MemoryAtom) -> tuple:
            s = score[a.atom_id]
            c = a.content or ""
            meta = 3 if re.match(r'^(一位|用户)', c) else 0
            age = a.atom_id / max_id  # 0=最旧 1=最新
            # 匹配数×0.2 + 重要度×0.5 + 年龄优势×0.3 - 元描述惩罚
            combined = s * 0.2 + a.importance * 0.5 + (1 - age) * 0.3 - meta * 0.1
            return (-combined, a.atom_id)

        sorted_atoms = sorted(atoms.values(), key=_sort_key)

        with open('/tmp/recall_debug.txt', 'a') as _f:
            _f.write(f"recall uid={user_id} matched={len(atoms)} returned={min(k, len(sorted_atoms))} keywords={keywords}\n")

        return sorted_atoms[:k]

    async def get_context_memories(
        self, user_id: str, query: str, k: int | None = None
    ) -> RecallResult:
        """
        生成供注入用的记忆文本

        返回 RecallResult，包含格式化的文本 + 原子列表 + 画像
        """
        k = k or self.recall_count
        atoms = await self.recall(user_id, query, k)
        persona = await self.persona_store.read(user_id)

        # 更新原子的访问时间
        for atom in atoms:
            await self.atom_store.touch(atom.atom_id)

        # 组装文本
        lines = []

        # 画像部分
        if persona:
            lines.append(f"【关于你】\n{persona[:300]}")

        # 原子部分
        if atoms:
            parts = []
            for a in atoms:
                date_part = f" ({a.diary_date})" if a.diary_date else ""
                parts.append(f"- [{a.atom_type.value}]{date_part} {a.content[:200]}")
            if parts:
                lines.append("【我记忆中最近的事】")
                lines.extend(parts)

        # 检查 token 预算
        text = "\n".join(lines)
        # 简单的 token 估算（中文 ~1.5 字/token，英文 ~4 字/token）
        if len(text) > self.recall_max_tokens * 4:
            # 裁剪过长文本
            text = text[: self.recall_max_tokens * 4] + "..."

        return RecallResult(
            memory_text=text,
            atoms=atoms,
            persona_text=persona,
        )

    async def search_diaries(self, user_id: str, query: str, k: int = 5) -> list[dict]:
        """搜索日记全文（diary_fts）"""
        if not self.diary_store:
            return []
        imp_w, rank_w = self._search_weights()
        return await self.diary_store.search_fts(query, user_id, k, imp_w, rank_w)

    async def hybrid_search(self, user_id: str, query: str, k: int = 5) -> dict:
        """混合搜索：原子 + 日记，合并返回"""
        atoms = await self.recall(user_id, query, k)
        diaries = await self.search_diaries(user_id, query, k)
        return {"atoms": atoms, "diaries": diaries}

    async def recall_by_keywords(
        self, user_id: str, keywords: list[str], k: int = 5
    ) -> list[MemoryAtom]:
        """按关键词列表搜索（多个关键词 OR 匹配）"""
        query = " ".join(keywords)
        return await self.recall(user_id, query, k)
