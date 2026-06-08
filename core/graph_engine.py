"""图谱引擎 — 从原子和日记构建知识图谱"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from ..models.graph_models import GraphNode, GraphEdge, ExtractedGraph
from ..models.memory_atom import MemoryAtom
from ..storage.graph_store import GraphStore
from ..storage.atom_store import AtomStore
from ..storage.diary_store import DiaryStore


class GraphEngine:
    """
    图谱引擎

    构建三类节点：
    - atom_type: 原子类型（episodic/factual/preference/planned/relational）
    - entity: 从原子内容提取的实体/话题
    - topic: 日记中的话题标签

    边的规则：
    - 同一条日记中的原子互相关联（same_diary）
    - 提到同一实体的原子互相关联（co_occur）
    """

    def __init__(
        self,
        graph_store: GraphStore,
        atom_store: AtomStore,
        diary_store: DiaryStore,
        config: dict[str, Any] | None = None,
    ):
        self.graph_store = graph_store
        self.atom_store = atom_store
        self.diary_store = diary_store
        self.config = config or {}

    async def index_atom(self, atom: MemoryAtom):
        """为单条原子建立图谱索引"""
        extracted = self._extract_from_atom(atom)
        if not extracted.nodes:
            return
        node_key_map = await self.graph_store.upsert_nodes(extracted.nodes)
        for edge in extracted.edges:
            await self.graph_store.add_edge(edge, node_key_map)

    async def index_diary(
        self,
        diary_id: int,
        content: str,
        entities: list[str] | None = None,
    ):
        """从日记 content 中解析 [[链接]] 并建立图谱索引

        这是图谱索引的主入口。写入日记时调用此方法，图谱自动同步：
        1. 创建日记节点 (node_type=diary)
        2. 创建实体节点 (node_type=entity/user)
        3. 创建 mentions 边 (entity → diary)
        4. 更新 co_occur 计数
        """
        from ..core.diary_helper import extract_wikilinks

        # 1. 提取 [[链接]] + 合并传入实体
        linked = extract_wikilinks(content)
        all_entities: list[str] = []
        seen: set[str] = set()
        for name in linked + (entities or []):
            name = name.strip()
            if name and name not in seen:
                seen.add(name)
                all_entities.append(name)

        # 1a. 创建日记节点
        # node_key = f"{node_type}:{canonical_value}" = "diary:diary_1"
        diary_nodes = [
            GraphNode(
                node_type="diary",
                value=f"#{diary_id}",
                canonical_value=f"diary_{diary_id}",
                metadata={"diary_id": diary_id},
            )
        ]
        node_key_map = await self.graph_store.upsert_nodes(diary_nodes)
        diary_key = f"diary:diary_{diary_id}"
        diary_node_id = node_key_map.get(diary_key, 0)

        if not all_entities:
            return

        # 2. 创建实体节点（检测 user 类型）
        from ..storage.atom_store import AtomStore
        nodes = []
        for name in all_entities:
            cv = self._canonicalize(name)
            # 检测是否已知用户
            ntype = "entity"
            try:
                if hasattr(self, 'atom_store') and self.atom_store:
                    row = await self.atom_store.fetchone(
                        "SELECT uid FROM user_identities WHERE display_name = ? LIMIT 1",
                        (name,),
                    )
                    if row:
                        ntype = "user"
            except Exception:
                pass
            nodes.append(GraphNode(
                node_type=ntype,
                value=name,
                canonical_value=cv,
                metadata={"diary_refs": 1},
            ))

        node_key_map.update(await self.graph_store.upsert_nodes(nodes))

        # 3. 创建 mentions 边 (entity → diary)，target 指向真实 diary 节点
        entity_ids = []
        for name in all_entities:
            cv = self._canonicalize(name)
            for prefix in ("entity:", "user:"):
                src_key = f"{prefix}{cv}"
                src_id = node_key_map.get(src_key)
                if src_id:
                    await self.graph_store.add_edge_by_ids(
                        edge_key=f"diary:{diary_id}:mentions:{cv}",
                        source_node_id=src_id,
                        target_node_id=diary_node_id,  # 指向真实日记节点 ✅
                        relation_type="mentions",
                        source_memory_id=diary_id,
                        weight=1.0,
                    )
                    entity_ids.append(src_id)
                    break

        # 4. 更新 co_occur 计数
        if entity_ids:
            await self.graph_store.update_cooccur(entity_ids)

        # 5. same_user 边：匹配到 user 节点的实体，查同一 UID 的其他平台 ID
        for name in all_entities:
            cv = self._canonicalize(name)
            for prefix in ("entity:", "user:"):
                src_id = node_key_map.get(f"{prefix}{cv}")
                if src_id:
                    break
            else:
                continue
            try:
                uid_row = await self.atom_store.fetchone("""
                    SELECT ui.uid FROM user_identities ui
                    JOIN graph_nodes gn ON gn.node_type='user'
                    WHERE gn.id=? AND ui.display_name=gn.value
                    LIMIT 1
                """, (src_id,))
                if not uid_row:
                    continue
                uid = uid_row[0]
                siblings = await self.atom_store.fetch("""
                    SELECT platform_id FROM user_identities
                    WHERE uid=? AND platform_id != ?
                """, (uid, f"user:{cv}"))
                for sib in siblings:
                    pid = sib[0]
                    sib_node = await self.graph_store.fetchone(
                        "SELECT id FROM graph_nodes WHERE canonical_value=? AND node_type='user'",
                        (self._canonicalize(pid),),
                    )
                    if sib_node and sib_node[0] != src_id:
                        await self.graph_store.add_edge_by_ids(
                            edge_key=f"same_user:{min(src_id,sib_node[0])}:{max(src_id,sib_node[0])}",
                            source_node_id=src_id,
                            target_node_id=sib_node[0],
                            relation_type="same_user",
                            source_memory_id=0,
                            weight=1.0,
                            confidence=1.0,
                        )
            except Exception:
                pass

    async def reindex_all(self, user_id: str | None = None):
        """重建全部图谱"""
        atoms = await self.atom_store.get_by_user(user_id) if user_id else []
        # 如果没有指定用户，获取所有
        if not user_id:
            all_ids = await self.atom_store.get_all_active_user_ids()
            atoms = []
            for uid in all_ids:
                atoms.extend(await self.atom_store.get_by_user(uid))

        for atom in atoms:
            try:
                await self.index_atom(atom)
            except Exception:
                pass

    def _extract_from_atom(self, atom: MemoryAtom) -> ExtractedGraph:
        """从单条原子提取节点和边"""
        graph = ExtractedGraph()
        node_map: dict[str, GraphNode] = {}

        def _add_node(ntype: str, value: str) -> str | None:
            if not value or not value.strip():
                return None
            cv = self._canonicalize(value)
            if not cv:
                return None
            node = GraphNode(
                node_type=ntype,
                value=value.strip()[:100],
                canonical_value=cv,
                metadata={"count": 1},
            )
            node_map[node.node_key] = node
            return node.node_key

        # 1. atom_type 节点
        type_key = _add_node("atom_type", atom.atom_type.value)
        if type_key and atom.content:
            # 从内容提取关键实体（2-4字词、专有名词）
            entities = self._extract_entities(atom.content)
            for entity in entities[:5]:  # 最多5个实体
                entity_key = _add_node("entity", entity)
                if entity_key and type_key:
                    # 实体 ↔ 类型 边
                    graph.edges.append(GraphEdge(
                        source_key=entity_key,
                        target_key=type_key,
                        relation_type="typed_as",
                        source_memory_id=atom.atom_id,
                        weight=atom.importance,
                    ))

            # 如果原子有 entities 字段，直接用
            for ent in (atom.entities or [])[:5]:
                if isinstance(ent, str) and ent.strip():
                    ent_key = _add_node("entity", ent)
                    if ent_key and type_key:
                        graph.edges.append(GraphEdge(
                            source_key=ent_key,
                            target_key=type_key,
                            relation_type="typed_as",
                            source_memory_id=atom.atom_id,
                            weight=atom.importance,
                        ))

        # 2. 同日记内的原子互相连接（通过 diary_date）
        if atom.diary_date:
            date_key = _add_node("topic", f"📅 {atom.diary_date}")
            if date_key and type_key:
                graph.edges.append(GraphEdge(
                    source_key=type_key,
                    target_key=date_key,
                    relation_type="on_date",
                    source_memory_id=atom.atom_id,
                    weight=0.5,
                ))

        graph.nodes.extend(node_map.values())
        return graph

    def _extract_entities(self, text: str) -> list[str]:
        """从文本提取可能的实体/话题"""
        entities = set()

        # 提取引号中的内容（如「告白」、「约定」）
        quotes = re.findall(r'[「」“”\'\"]+([^「」“”\'\"]+)[「」“”\'\"]+', text)
        for q in quotes:
            q = q.strip()
            if len(q) >= 2 and len(q) <= 30:
                entities.add(q)

        # 提取英文专有名词/项目名（如 GPT-SoVITS, Arch Linux）
        proj = re.findall(r'[A-Z][a-zA-Z0-9_-]{2,}(?:\s+[A-Z][a-zA-Z0-9_-]+)*', text)
        for p in proj:
            if len(p) >= 3:
                entities.add(p.strip())

        # 提取中文关键词组合（2-6字）
        words = re.findall(r'[一-鿿]{2,6}', text)
        # 过滤常见停用词
        stopwords = {"可以", "这个", "那个", "什么", "怎么", "我们", "一个", "没有", "不是", "但是", "因为", "所以", "如果", "自己", "知道", "感觉", "时候", "东西", "一下", "之后", "就是", "这样", "那个", "已经"}
        for w in words:
            if w not in stopwords and len(w) >= 2:
                entities.add(w)

        return list(entities)[:10]

    def _canonicalize(self, value: str) -> str:
        """归一化：小写、去空格、统一字符"""
        v = value.lower().strip()
        v = re.sub(r'\s+', '_', v)
        # 中文特殊字符统一
        v = v.replace('（', '(').replace('）', ')').replace('：', ':').replace('；', ';')
        return v[:80]
