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
from ..retrieval import MultiRouteRetriever, BM25Retriever, GraphKeywordRetriever, GraphVectorRetriever, VectorRetriever
from ..core.adapters import EmbeddingProvider
from .interfaces import IRetriever


class Retriever(IRetriever):
    """
    记忆检索引擎

    双路检索 + RRF 融合：
    - BM25 文档路：memory_atoms_fts FTS5 全文检索
    - Graph 图路：entity → graph_edges → diary → fact

    热缓存：
    - get_recent_context() 从 ConversationStore 读取
    - 未命中时回退到 ConversationStore 查库
    """

    def __init__(
        self,
        atom_store: AtomStore,
        persona_store: PersonaStore,
        diary_store: DiaryStore | None = None,
        config: dict[str, Any] | None = None,
        conversation_store=None,
        graph_store: GraphStore | None = None,
        embed_provider: EmbeddingProvider | None = None,
    ):
        self.atom_store = atom_store
        self.persona_store = persona_store
        self.diary_store = diary_store
        self.config = config or {}
        self.conversation_store = conversation_store
        self.recall_count = self.config.get("recall_count", 5)
        self.recall_max_tokens = self.config.get("recall_max_tokens", 500)
        self.embed_provider = embed_provider

        # 多路检索引擎（双路四模式）
        self.multi_route: MultiRouteRetriever | None = None
        if graph_store:
            keyword_retriever = GraphKeywordRetriever(graph_store, atom_store)
            vector_retriever = (
                VectorRetriever(atom_store, embed_provider) if embed_provider else None
            )
            graph_vector_retriever = (
                GraphVectorRetriever(graph_store, atom_store, embed_provider) if embed_provider else None
            )
            self.multi_route = MultiRouteRetriever(
                bm25_retriever=BM25Retriever(atom_store),
                graph_keyword_retriever=keyword_retriever,
                vector_retriever=vector_retriever,
                graph_vector_retriever=graph_vector_retriever,
            )
        # 向后兼容
        self.dual_route = self.multi_route

    # ── RRF 融合 ──
    RRF_K = 60

    @staticmethod
    def _search_weights() -> tuple[float, float]:
        """搜索权重配置（用于 diary FTS 排序）"""
        return 0.6, 0.4

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

        流程：
        1. 召回 top-k 原子（当前检索逻辑）
        2. 取最相关原子 → 回溯关联日记 → 选最佳段落
        3. 画像改用 tags + 一句话摘要（替代旧版全文）

        返回 RecallResult 包含格式化的文本 + 原子 + 日记溯源
        """
        from ..utils.context_formatter import format_date_tag

        k = k or self.recall_count
        atoms = await self.recall(user_id, query, k)

        # 更新原子的访问时间
        for atom in atoms:
            await self.atom_store.touch(atom.atom_id)

        # ── 画像：改用 tags + 摘要（替代旧 PersonaStore Markdown） ──
        persona_tags = []
        persona_summary = ""
        try:
            persona_tags = await self.atom_store.get_persona_tags(user_id)
            persona_summary = await self.atom_store.get_persona_summary(user_id)
        except Exception:
            pass
        persona_text = ""
        if persona_tags:
            persona_text = f"标签: {', '.join(persona_tags[:8])}"
        if persona_summary:
            summary_short = persona_summary[:100].split("\n")[0].strip()
            if summary_short:
                persona_text = f"{persona_text}\n摘要: {summary_short}" if persona_text else f"摘要: {summary_short}"

        # ── 原子回溯日记：遍历 top 原子，收集不重复的日记段落 ──
        max_diaries = int(self.config.get("injection_max_diaries", 2))
        diary_refs: list[dict] = []
        if atoms and max_diaries > 0:
            seen_diary_ids: set[int] = set()
            for atom in atoms:
                if len(diary_refs) >= max_diaries:
                    break
                diary_ids = await self._find_atom_diaries(atom)
                for did in diary_ids:
                    if did in seen_diary_ids:
                        continue
                    seen_diary_ids.add(did)
                    seg = await self._best_diary_segment([did], atom.content, query)
                    if seg:
                        diary_refs.append(seg)
                        if len(diary_refs) >= max_diaries:
                            break

        # ── 组装文本 ──
        lines = []
        now = time.time()

        # 画像
        if persona_text:
            lines.append(persona_text)

        # 原子列表
        if atoms:
            parts = []
            for a in atoms:
                tag = format_date_tag(a.diary_date, now)
                date_tag = f" [{tag}]" if tag else ""
                parts.append(f"- [{a.atom_type.value}]{date_tag} {a.content[:200]}")
            if parts:
                lines.append("")
                lines.append("📌 事实")
                lines.extend(parts)

        # 日记溯源
        if diary_refs:
            lines.append("")
            lines.append("📖 溯源原文")
            for dr in diary_refs:
                lines.append(f"- [{dr['date']} 日记#{dr['diary_id']}] {dr['snippet']}")

        # 搜索提示
        lines.append("")
        lines.append("💡 如需查看更多，可以调 search_memories 或 read_diary 工具。")

        text = "\n".join(lines)

        # Token 预算控制
        max_chars = self.recall_max_tokens * 4
        if len(text) > max_chars:
            text = text[:max_chars] + "..."

        return RecallResult(
            memory_text=text,
            atoms=atoms,
            persona_text="",  # 已嵌入 memory_text，避免 injector 重复
            diary_refs=diary_refs,
        )

    async def _find_atom_diaries(self, atom: MemoryAtom) -> list[int]:
        """给定原子，找到所有关联的日记 ID（通过 atoms_diary_links 桥表）"""
        diary_ids: list[int] = []
        seen: set[int] = set()

        try:
            rows = await self.atom_store.fetch(
                """SELECT diary_id, importance FROM atoms_diary_links
                   WHERE atom_id = ? ORDER BY importance DESC LIMIT 10""",
                (atom.atom_id,),
            )
            for r in rows:
                did = r[0]
                if did not in seen:
                    seen.add(did)
                    diary_ids.append(did)
        except Exception:
            pass

        return diary_ids

    async def _best_diary_segment(
        self, diary_ids: list[int], atom_content: str, query: str, max_len: int = 150,
    ) -> dict | None:
        """从多篇日记中找与原子内容最相关的段落

        Args:
            diary_ids: 候选日记 ID 列表
            atom_content: 原子内容（作为匹配基准）
            query: 用户查询
            max_len: 段落最大字数

        Returns:
            {diary_id, date, snippet} 或 None
        """
        if not diary_ids or not self.diary_store:
            return None

        import jieba
        import re

        atom_words = set(jieba.lcut(atom_content)) if atom_content else set()
        query_words = set(jieba.lcut(query)) if query else set()

        best_score = 0.3  # 最低匹配阈值
        best_result = None

        for did in diary_ids:
            row = await self.diary_store.fetchone(
                "SELECT date, content FROM diary_entries WHERE id=?", (did,)
            )
            if not row:
                continue

            date_str, content = row[0] or "", row[1] or ""

            # 去掉 frontmatter
            if content.startswith("---"):
                end = content.find("\n---", 3)
                if end != -1:
                    content = content[end + 5:].strip()

            # 分句
            segments = re.split(r'(?<=[。！？\n])', content)

            for seg in segments:
                seg = seg.strip()
                if len(seg) < 15:
                    continue
                seg_words = set(jieba.lcut(seg))
                if not seg_words:
                    continue

                a_ov = (len(atom_words & seg_words) / len(atom_words | seg_words)
                        if atom_words else 0)
                q_ov = (len(query_words & seg_words) / len(query_words | seg_words)
                        if query_words else 0)
                score = a_ov * 0.6 + q_ov * 0.4

                if score > best_score:
                    best_score = score
                    best_result = {
                        "diary_id": did,
                        "date": date_str,
                        "snippet": seg[:max_len],
                    }

        return best_result

    async def get_recent_context(
        self, user_id: str, session_id: str = "", limit: int = 20, bot_name: str = "我"
    ) -> str:
        """获取最近对话上下文 — 从 ConversationStore 读取"""
        if self.conversation_store and session_id:
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
