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
from ..core.interfaces import IGraphEngine


class GraphEngine(IGraphEngine):
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
        from ..utils.diary_helper import extract_wikilinks

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

        # 1b. 解析 frontmatter → topic/emotion/date 节点
        from ..utils.diary_helper import parse_diary_content
        fm, _ = parse_diary_content(content)
        meta_nodes: list[GraphNode] = []
        date_str = fm.get("date", "")
        if date_str:
            meta_nodes.append(GraphNode(
                node_type="date", value=date_str,
                canonical_value=date_str.replace("-", ""),
                metadata={"count": 1},
            ))
        mood = fm.get("mood", "")
        if mood:
            meta_nodes.append(GraphNode(
                node_type="emotion", value=mood,
                canonical_value=mood.lower().strip(),
                metadata={"count": 1},
            ))
        for topic in (fm.get("topics") or []):
            t = str(topic).strip()
            if t:
                meta_nodes.append(GraphNode(
                    node_type="topic", value=t,
                    canonical_value=self._canonicalize(t),
                    metadata={"count": 1},
                ))
        if meta_nodes:
            node_key_map.update(await self.graph_store.upsert_nodes(meta_nodes))

        if not all_entities:
            # 尽管无实体，仍需将日记关联到日期等元节点
            for mn in meta_nodes:
                mn_key = mn.node_key
                mn_id = node_key_map.get(mn_key)
                if mn_id and diary_node_id:
                    await self.graph_store.add_edge_by_ids(
                        edge_key=f"diary:{diary_id}:meta:{mn.node_type}",
                        source_node_id=diary_node_id,
                        target_node_id=mn_id,
                        relation_type="on_date" if mn.node_type == "date" else "has_meta",
                        source_memory_id=diary_id,
                        weight=0.5,
                    )
            return

        # 2. 创建实体节点（检测 user 类型）
        # 并将 meta 节点也加入 node_key_map 供后续关联使用
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

        # 3a. 关联日记 → topic/emotion/date 元节点
        for mn_key in [f"{t}:{cv}" for t, cv in [
            ("date", date_str.replace("-", "") if date_str else ""),
        ] if cv]:
            mn_id = node_key_map.get(mn_key)
            if mn_id and diary_node_id:
                await self.graph_store.add_edge_by_ids(
                    edge_key=f"diary:{diary_id}:on_date:{mn_key.split(':')[-1]}",
                    source_node_id=diary_node_id,
                    target_node_id=mn_id,
                    relation_type="on_date",
                    source_memory_id=diary_id,
                    weight=0.5,
                )
        for mood_val in [mood] if mood else []:
            mk = f"emotion:{mood_val.lower().strip()}"
            mn_id = node_key_map.get(mk)
            if mn_id and diary_node_id:
                await self.graph_store.add_edge_by_ids(
                    edge_key=f"diary:{diary_id}:mood:{mood_val.lower().strip()}",
                    source_node_id=diary_node_id,
                    target_node_id=mn_id,
                    relation_type="has_meta",
                    source_memory_id=diary_id,
                    weight=0.5,
                )
        for topic in (fm.get("topics") or []):
            t = str(topic).strip()
            if t:
                tk = f"topic:{self._canonicalize(t)}"
                tn_id = node_key_map.get(tk)
                if tn_id and diary_node_id:
                    await self.graph_store.add_edge_by_ids(
                        edge_key=f"diary:{diary_id}:topic:{self._canonicalize(t)}",
                        source_node_id=diary_node_id,
                        target_node_id=tn_id,
                        relation_type="has_meta",
                        source_memory_id=diary_id,
                        weight=0.5,
                    )

        # 4. same_user 边：匹配到 user 节点的实体，查同一 UID 的其他平台 ID
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

    async def upgrade_cooccur_to_relates(self, min_count: int = 3) -> int:
        """将高频共现实体对升级为 relates_to 语义关联边

        扫描 entity_cooccur，count >= min_count 且尚无 relates_to 边的，
        创建 relation_type="relates_to" 的边。
        返回创建的边数。
        """
        pairs = await self.graph_store.fetch("""
            SELECT ec.entity_a_id, ec.entity_b_id, ec.count
            FROM entity_cooccur ec
            WHERE ec.count >= ?
        """, (min_count,))
        if not pairs:
            return 0

        created = 0
        for a_id, b_id, cnt in pairs:
            # 检查是否已有 relates_to 边
            key1 = f"relates_to:{min(a_id,b_id)}:{max(a_id,b_id)}"
            existing = await self.graph_store.fetchone(
                "SELECT id FROM graph_edges WHERE edge_key=?", (key1,)
            )
            if existing:
                continue

            # 检查两个节点是否存在
            na = await self.graph_store.fetchone(
                "SELECT id, value FROM graph_nodes WHERE id=?", (a_id,)
            )
            nb = await self.graph_store.fetchone(
                "SELECT id, value FROM graph_nodes WHERE id=?", (b_id,)
            )
            if not na or not nb:
                continue

            weight = min(1.0, cnt * 0.15)
            await self.graph_store.add_edge_by_ids(
                edge_key=key1,
                source_node_id=a_id,
                target_node_id=b_id,
                relation_type="relates_to",
                source_memory_id=0,
                weight=weight,
                confidence=0.6,
            )
            created += 1

        return created

    async def batch_cooccur(self) -> int:
        """批量重建 co_occur 统计（从 graph_edges mentions 边聚合）

        每天后台运行一次，替代实时的 update_cooccur 调用。
        返回写入的实体对总数。
        注意：在单事务内执行 DELETE+INSERT，保证原子性。
        """
        # 按 diary 分组获取所有提及的实体
        rows = await self.graph_store.fetch("""
            SELECT ge.source_memory_id, ge.source_node_id
            FROM graph_edges ge
            WHERE ge.relation_type = 'mentions' AND ge.source_memory_id > 0
            ORDER BY ge.source_memory_id, ge.source_node_id
        """)

        # 按 diary 分组
        from collections import defaultdict
        diary_entities: dict[int, list[int]] = defaultdict(list)
        for r in rows:
            diary_id, node_id = r[0], r[1]
            if node_id not in diary_entities[diary_id]:
                diary_entities[diary_id].append(node_id)

        # 统计共现
        cooccur_counts: dict[tuple[int, int], int] = {}
        for entities in diary_entities.values():
            for i in range(len(entities)):
                for j in range(i + 1, len(entities)):
                    a, b = entities[i], entities[j]
                    if a == b:
                        continue
                    key = (min(a, b), max(a, b))
                    cooccur_counts[key] = cooccur_counts.get(key, 0) + 1

        # 单事务：DELETE + INSERT（保证原子性）
        now = self.graph_store._now_iso()
        async with self.graph_store._connect() as db:
            await db.execute("DELETE FROM entity_cooccur")
            for (a, b), count in cooccur_counts.items():
                await db.execute("""
                    INSERT INTO entity_cooccur (entity_a_id, entity_b_id, count, last_updated)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(entity_a_id, entity_b_id)
                    DO UPDATE SET count = excluded.count, last_updated = excluded.last_updated
                """, (a, b, count, now))
            await db.commit()

        return len(cooccur_counts)

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
