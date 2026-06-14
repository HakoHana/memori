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
        self.candidate_k = self.config.get("candidate_k", 50)   # 每路候选数
        self.recall_max_tokens = self.config.get("recall_max_tokens", 500)

        # 社交加权
        self.social_alpha = self.config.get("social_alpha", 0.3)             # 增强系数
        self.embed_provider = embed_provider
        self.graph_store = graph_store

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
        """长期记忆召回（推荐算法风格）

        流程：
        1.  多路检索（全库，不限 user_id），每路返回 candidate_k 条
        2.  RRF 融合去重
        3.  社交加权重排序（软加权，不丢弃）
        4.  取 top-k 返回
        """
        k = k or self.recall_count
        if not query or not query.strip():
            return []

        keywords = self._keyword_list(query)
        candidate_k = self.candidate_k

        # ── 1. 多路检索（全库，空 user_ids = 不限 user_id）──
        atoms: list[MemoryAtom] = []
        if self.multi_route:
            atoms = await self.multi_route.retrieve(keywords, [], candidate_k)

        # ── 2. 兜底：全库重要度降序（填补多路未覆盖的）──
        if len(atoms) < candidate_k:
            seen_ids = {a.atom_id for a in atoms}
            try:
                rows = await self.atom_store.fetch(
                    "SELECT * FROM memory_atoms WHERE status='active' "
                    "ORDER BY importance DESC, id DESC LIMIT ?",
                    (candidate_k,),
                )
                for r in rows:
                    atom = self.atom_store._row_to_atom(r)
                    if atom.atom_id not in seen_ids:
                        atoms.append(atom)
                        seen_ids.add(atom.atom_id)
                        if len(atoms) >= candidate_k:
                            break
            except Exception:
                pass

        # ── 3. 社交加权重排序 ──
        atoms = await self._social_rerank(atoms, user_id)

        # ── 4. 截取最终 top-k ──
        return atoms[:k]

    async def _social_rerank(
        self, atoms: list[MemoryAtom], user_id: str
    ) -> list[MemoryAtom]:
        """社交加权重排序（乘法增强，不丢弃）

        策略（按架构图）：
        - 从候选原子提取唯一 user_id
        - 一次性查询 graph_edges 双向匹配（from_node OR to_node）
        - 自己 weight = 1.0，朋友取 edge.weight，缺失 = 0.0
        - 乘法公式：final_score = relevance × (1 + α × weight)
        """
        if not atoms or not self.graph_store or not user_id:
            return atoms

        alpha = getattr(self, 'social_alpha', 0.3)

        # 1. 从候选原子提取唯一 user_id
        candidate_uids = list({a.user_id for a in atoms if a.user_id})

        # 2. 批量查 graph_edges（双向匹配），只查这些 user_id 中哪些是朋友
        friend_weights: dict[str, float] = {}
        try:
            other_uids = [u for u in candidate_uids if u != user_id]
            if other_uids:
                uid_node = f"user:{user_id}"
                other_nodes = [f"user:{u}" for u in other_uids]
                placeholders = ",".join("?" for _ in other_nodes)
                rows = await self.graph_store.fetch(
                    f"""SELECT
                            CASE WHEN from_node = ? THEN to_node ELSE from_node END AS friend_node,
                            weight
                        FROM edges
                        WHERE relation_type = 'friend_of'
                          AND (? IN (from_node, to_node))
                          AND (from_node IN ({placeholders}) OR to_node IN ({placeholders}))""",
                    (uid_node, uid_node, *other_nodes, *other_nodes),
                )
                for r in rows:
                    uid = r[0].split(":", 1)[1]  # "user:u_xxx" → "u_xxx"
                    friend_weights[uid] = max(friend_weights.get(uid, 0.0), r[1])
        except Exception:
            pass

        # 3. 乘法公式重排序
        n = len(atoms)
        scored: list[tuple[float, int, MemoryAtom]] = []

        for idx, atom in enumerate(atoms):
            relevance = 1.0 - (idx / max(1, n - 1)) if n > 1 else 1.0

            if atom.user_id == user_id:
                boost = 1.0
            elif atom.user_id in friend_weights:
                boost = friend_weights[atom.user_id]
            else:
                boost = 0.0

            final_score = relevance * (1.0 + alpha * boost)
            scored.append((final_score, idx, atom))

        scored.sort(key=lambda x: (-x[0], x[1]))
        return [s[2] for s in scored]

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

        # ── 原子回溯日记：批量一次 IN 获取 ──
        max_diaries = int(self.config.get("injection_max_diaries", 2))
        diary_refs: list[dict] = []
        if atoms and max_diaries > 0:
            try:
                atom_ids = [a.atom_id for a in atoms if a.atom_id]
                # 1. 批量查 atoms_diary_links
                placeholders = ",".join("?" for _ in atom_ids)
                link_rows = await self.atom_store.fetch(
                    f"""SELECT atom_id, diary_id, importance, snippet
                        FROM atoms_diary_links
                        WHERE atom_id IN ({placeholders})
                        ORDER BY importance DESC""",
                    atom_ids,
                )
                if link_rows:
                    # 2. 去重取 top diary_id
                    seen_dids: set[int] = set()
                    top_diary_ids: list[int] = []
                    for lr in link_rows:
                        did = lr[1]
                        if did not in seen_dids:
                            seen_dids.add(did)
                            top_diary_ids.append(did)
                            if len(top_diary_ids) >= max_diaries:
                                break

                    # 3. 批量查日记内容
                    if top_diary_ids:
                        d_placeholders = ",".join("?" for _ in top_diary_ids)
                        diary_rows = await self.diary_store.fetch(
                            f"SELECT id, date, content FROM diary_entries WHERE id IN ({d_placeholders})",
                            top_diary_ids,
                        )
                        diary_map = {r[0]: (r[1] or "", r[2] or "") for r in diary_rows}

                        # 构建 atom 内容映射（用于段落匹配）
                        atom_content_map = {a.atom_id: a.content for a in atoms}

                        # 4. 对每个 top diary 选最佳段落
                        for did in top_diary_ids:
                            if did not in diary_map:
                                continue
                            date_str, content = diary_map[did]
                            # 用关联到这篇日记的原子中、内容最相关的那个做匹配
                            linked_atoms = [lr for lr in link_rows if lr[1] == did]
                            best_atom_content = ""
                            for lr in linked_atoms:
                                ac = atom_content_map.get(lr[0], "")
                                if len(ac) > len(best_atom_content):
                                    best_atom_content = ac
                            seg = self._pick_best_segment(
                                content, best_atom_content, query
                            )
                            if seg:
                                diary_refs.append({
                                    "diary_id": did,
                                    "date": date_str,
                                    "snippet": seg[:200],
                                })
            except Exception:
                pass

        # ── 组装文本 ──
        lines = []
        now = time.time()

        # 原子内容中的 bot 名统一替换为第一人称
        bot_name = self.config.get("bot_name", "Hana")

        def _replace_bot(text: str) -> str:
            for old in (bot_name, "Bot"):
                text = text.replace(old, "我")
            return text

        # 画像
        if persona_text:
            lines.append(persona_text)

        # 原子列表
        if atoms:
            parts = []
            for a in atoms:
                tag = format_date_tag(a.diary_date, now)
                date_tag = f" [{tag}]" if tag else ""
                content = _replace_bot(a.content[:200])
                parts.append(f"- [{a.atom_type.value}]{date_tag} {content}")
            if parts:
                lines.append("")
                lines.append("📌 事实")
                lines.extend(parts)

        # 日记溯源
        if diary_refs:
            lines.append("")
            lines.append("📖 溯源原文")
            for dr in diary_refs:
                snippet = _replace_bot(dr["snippet"])
                lines.append(f"- [{dr['date']} 日记#{dr['diary_id']}] {snippet}")

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

    @staticmethod
    def _pick_best_segment(
        diary_content: str, atom_content: str, query: str, max_len: int = 200,
    ) -> str | None:
        """提取日记中含关键词的段落（带前后句上下文）

        策略：
        1. 从 query 和原子内容中提取关键词
        2. 日记分句，找含关键词最多的句子
        3. 带上该句的前后各一句作为上下文
        4. 无匹配则返回前 80 字作为上下文兜底
        """
        if not diary_content or not diary_content.strip():
            return None

        import re

        # 去掉 frontmatter
        content = diary_content
        if content.startswith("---"):
            end = content.find("\n---", 3)
            if end != -1:
                content = content[end + 5:].strip()

        # 从 query 和原子内容收集关键词
        keywords: set[str] = set()
        if query:
            keywords.update(Retriever._keyword_list(Retriever, query))
        if atom_content:
            keywords.update(Retriever._keyword_list(Retriever, atom_content))

        # 按句号、感叹号、问号、换行分句
        segments = re.split(r'(?<=[。！？\n])', content)
        segments = [s.strip() for s in segments if s.strip() and len(s.strip()) >= 4]

        if not segments:
            return None

        if not keywords:
            return segments[0][:max_len]

        # 找含关键词最多的句子
        best_idx = 0
        best_count = 0

        for i, seg in enumerate(segments):
            seg_lower = seg.lower()
            count = sum(1 for kw in keywords if kw.lower() in seg_lower)
            if count > best_count:
                best_count = count
                best_idx = i

        # 取命中句 + 前后各一句
        start = max(0, best_idx - 1)
        end = min(len(segments), best_idx + 2)
        result = "".join(segments[start:end])

        if len(result) > max_len:
            return result[:max_len]
        return result if result else segments[0][:max_len]

    async def get_recent_context(
        self, user_id: str, session_id: str = "", limit: int = 20, bot_name: str = "我"
    ) -> str:
        """获取最近对话上下文 — 从 ConversationStore 读取"""
        if self.conversation_store and session_id:
            return await self.conversation_store.get_recent_context(
                session_id, limit, bot_name
            )
        return ""

    async def search_diaries(self, query: str, k: int = 5) -> list[dict]:
        """搜索日记全文（diary_fts，全库搜索）"""
        if not self.diary_store:
            return []
        imp_w, rank_w = self._search_weights()
        return await self.diary_store.search_fts(query, k, imp_w, rank_w)

    async def hybrid_search(self, user_id: str, query: str, k: int = 5) -> dict:
        """混合搜索：原子 + 日记，合并返回"""
        atoms = await self.recall(user_id, query, k)
        diaries = await self.search_diaries(query, k)
        return {"atoms": atoms, "diaries": diaries}

    async def recall_by_keywords(
        self, user_id: str, keywords: list[str], k: int = 5
    ) -> list[MemoryAtom]:
        """按关键词列表搜索（多个关键词 OR 匹配）"""
        query = " ".join(keywords)
        return await self.recall(user_id, query, k)
