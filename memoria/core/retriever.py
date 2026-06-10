"""检索引擎 — 召回相关记忆（双路检索：BM25 文档路 + Graph 图路 + RRF 融合）"""

from __future__ import annotations

import re
import time
from typing import Any

import jieba

from ..models.memory_atom import MemoryAtom, RecallResult
from ..storage.atom_store import AtomStore
from ..storage.diary_store import DiaryStore
from ..storage.persona_store import PersonaStore
from ..storage.graph_store import GraphStore
from .logger import logger
from ..retrieval import DualRouteRetriever, BM25Retriever, GraphEntityRetriever


class Retriever:
    """
    记忆检索引擎

    双路检索 + RRF 融合：
    - BM25 文档路：memory_atoms_fts FTS5 全文检索
    - Graph 图路：entity → graph_edges → diary → fact

    热缓存：
    - get_recent_context() 优先从 HotMessageCache 读取
    - 未命中时回退到 ConversationStore 查库
    """

    def __init__(
        self,
        atom_store: AtomStore,
        persona_store: PersonaStore,
        diary_store: DiaryStore | None = None,
        config: dict[str, Any] | None = None,
        hot_cache=None,
        conversation_store=None,
        graph_store: GraphStore | None = None,
    ):
        self.atom_store = atom_store
        self.persona_store = persona_store
        self.diary_store = diary_store
        self.config = config or {}
        self.hot_cache = hot_cache
        self.conversation_store = conversation_store
        self.recall_count = self.config.get("recall_count", 5)
        self.recall_max_tokens = self.config.get("recall_max_tokens", 500)

        # 双路检索引擎（图路需要 graph_store）
        self.dual_route: DualRouteRetriever | None = None
        if graph_store:
            self.dual_route = DualRouteRetriever(
                bm25_retriever=BM25Retriever(atom_store),
                graph_retriever=GraphEntityRetriever(graph_store, atom_store),
            )

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
        """提取关键词：jieba 中文分词 + 纯英文/数字保持原样 + 停用词过滤"""
        tokens = self._segment(text)
        keywords = []
        for token in tokens:
            if re.match(r'^[a-z0-9_]+$', token.lower()):
                # 纯英文/数字词作为整体
                word = token.lower()
                if word not in self._ZH_STOP:
                    keywords.append(word)
            elif len(token) >= 2:
                # 中文：jieba 精确分词，过滤停用词和单字
                words = jieba.lcut(token)
                for w in words:
                    w = w.strip()
                    if len(w) >= 2 and w not in self._ZH_STOP:
                        keywords.append(w)
        # 去重 + 截断
        seen = set()
        return [k for k in keywords if not (k in seen or seen.add(k))][:15]

    async def recall(self, user_id: str, query: str, k: int | None = None) -> list[MemoryAtom]:
        """召回：双路检索（BM25 文档路 + Graph 图路）→ RRF 融合

        图路未就绪时回退到单 BM25 检索，
        全未命中时按重要度降序兜底。
        """
        k = k or self.recall_count
        if not query or not query.strip():
            return []

        extra_ids = await self.atom_store.get_related_user_ids(user_id)
        all_uids = list(set([user_id] + extra_ids))
        keywords = self._keyword_list(query)

        # ── 双路检索 ──
        atoms: list[MemoryAtom] = []
        if self.dual_route:
            atoms = await self.dual_route.retrieve(keywords, all_uids, k)

        # ── 双路未命中时，重要度降序回退 ──
        if len(atoms) < k:
            seen_ids = {a.atom_id for a in atoms}
            for uid in all_uids:
                try:
                    rows = await self.atom_store.fetch(
                        "SELECT * FROM memory_atoms WHERE user_id=? AND status='active' ORDER BY importance DESC, id DESC LIMIT ?",
                        (uid, k * 3),
                    )
                    for r in rows:
                        atom = self.atom_store._row_to_atom(r)
                        if atom.atom_id not in seen_ids:
                            atoms.append(atom)
                            seen_ids.add(atom.atom_id)
                            if len(atoms) >= k:
                                break
                except Exception:
                    pass

        with open('/tmp/recall_debug.txt', 'a') as _f:
            _f.write(f"recall uid={user_id} returned={len(atoms)} keywords={keywords}\n")
            if atoms:
                _f.write(f"  first: {atoms[0].content[:50]}\n")

        return atoms[:k]

    async def get_context_memories(
        self, user_id: str, query: str, k: int | None = None
    ) -> RecallResult:
        """
        生成供注入用的记忆文本

        返回 RecallResult，包含格式化的文本 + 原子列表 + 画像
        """
        from .context_formatter import format_date_tag

        k = k or self.recall_count
        atoms = await self.recall(user_id, query, k)
        persona = await self.persona_store.read(user_id)

        # 更新原子的访问时间
        for atom in atoms:
            await self.atom_store.touch(atom.atom_id)

        # 组装文本
        lines = []
        now = time.time()

        # 画像部分
        if persona:
            lines.append(f"【关于你】\n{persona[:300]}")

        # 原子部分 — 带时间标签
        if atoms:
            parts = []
            for a in atoms:
                tag = format_date_tag(a.diary_date, now)
                date_tag = f" [{tag}]" if tag else ""
                parts.append(f"- [{a.atom_type.value}]{date_tag} {a.content[:200]}")
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

    async def get_recent_context(
        self, user_id: str, session_id: str = "", limit: int = 20, bot_name: str = "我"
    ) -> str:
        """获取最近对话上下文 — 优先从热缓存读取

        热缓存命中 → 直接返回格式化文本（零 I/O）
        热缓存未命中 / 条数不足 → 回退查 ConversationStore
        """
        # 1) 优先从内存缓存读
        if self.hot_cache:
            cached = self.hot_cache.format_recent_context(user_id, limit, bot_name)
            if cached:
                logger.debug(f"[Cache] HOT HIT user={user_id} lines={len(cached.split(chr(10)))}")
                return cached
            logger.debug(f"[Cache] HOT MISS user={user_id}")

        # 2) 回退查库
        if self.conversation_store and session_id:
            logger.debug(f"[Cache] DB FALLBACK user={user_id}")
            return await self.conversation_store.get_recent_context(
                session_id, limit, bot_name
            )

        return ""

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
