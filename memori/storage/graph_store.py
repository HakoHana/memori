"""图谱存储 — SQLite 实现"""

from __future__ import annotations

import json
import time
from typing import Any

from ..models.graph_models import GraphNode, GraphEdge
from .base_store import BaseDbStore


class GraphStore(BaseDbStore):
    """持久化图谱节点和边"""
    _pragmas = ["PRAGMA journal_mode = WAL", "PRAGMA foreign_keys = ON"]
    _busy_timeout_ms = 10000

    async def initialize(self):
        async with self._connect() as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS graph_nodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    node_key TEXT NOT NULL UNIQUE,
                    node_type TEXT NOT NULL,
                    value TEXT NOT NULL,
                    canonical_value TEXT NOT NULL,
                    metadata TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            # 为已有数据库补充 embedding 列
            for col_def in [
                "embedding BLOB",
                "embedding_model TEXT DEFAULT ''",
            ]:
                col_name = col_def.split()[0]
                try:
                    cols = await db.execute_fetchall(
                        "SELECT name FROM pragma_table_info('graph_nodes') WHERE name=?",
                        (col_name,),
                    )
                    if not cols:
                        await db.execute(f"ALTER TABLE graph_nodes ADD COLUMN {col_def}")
                except Exception:
                    pass
            await db.execute("""
                CREATE TABLE IF NOT EXISTS graph_edges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    edge_key TEXT NOT NULL UNIQUE,
                    semantic_key TEXT NOT NULL,
                    source_node_id INTEGER NOT NULL,
                    target_node_id INTEGER NOT NULL,
                    relation_type TEXT NOT NULL,
                    source_memory_id INTEGER NOT NULL,
                    weight REAL NOT NULL DEFAULT 1.0,
                    confidence REAL NOT NULL DEFAULT 0.8,
                    status TEXT NOT NULL DEFAULT 'active',
                    metadata TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(source_node_id) REFERENCES graph_nodes(id) ON DELETE CASCADE,
                    FOREIGN KEY(target_node_id) REFERENCES graph_nodes(id) ON DELETE CASCADE
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_graph_nodes_type
                ON graph_nodes(node_type, canonical_value)
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_graph_edges_semantic
                ON graph_edges(semantic_key)
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_graph_edges_memory
                ON graph_edges(source_memory_id)
            """)
            # FTS5 全文索引（同步 graph_nodes.value）
            try:
                await db.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS graph_nodes_fts
                    USING fts5(value, content=graph_nodes, tokenize='unicode61')
                """)
                try:
                    await db.execute("INSERT INTO graph_nodes_fts(graph_nodes_fts) VALUES('rebuild')")
                except Exception:
                    pass
            except Exception:
                pass
            await db.execute("""
                CREATE TABLE IF NOT EXISTS entity_cooccur (
                    entity_a_id INTEGER NOT NULL,
                    entity_b_id INTEGER NOT NULL,
                    count INTEGER DEFAULT 1,
                    last_updated TEXT,
                    PRIMARY KEY (entity_a_id, entity_b_id)
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_cooccur_a ON entity_cooccur(entity_a_id)
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_cooccur_b ON entity_cooccur(entity_b_id)
            """)
            await db.commit()

    async def upsert_node(self, node: GraphNode) -> int:
        now = self._now_iso()
        async with self._connect() as db:
            cursor = await db.execute("""
                INSERT INTO graph_nodes (node_key, node_type, value, canonical_value, metadata, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(node_key) DO UPDATE SET
                    updated_at = excluded.updated_at,
                    metadata = CASE
                        WHEN json_valid(graph_nodes.metadata) AND json_valid(excluded.metadata)
                        THEN (SELECT json_object(
                            'count', COALESCE(json_extract(graph_nodes.metadata, '$.count'), 0) + 1
                        ))
                        ELSE excluded.metadata
                    END
            """, (node.node_key, node.node_type, node.value, node.canonical_value,
                  json.dumps(node.metadata), now, now))
            await db.commit()
            return cursor.lastrowid

    async def upsert_nodes(self, nodes: list[GraphNode]) -> dict[str, int]:
        key_map = {}
        async with self._connect() as db:
            now = self._now_iso()
            for node in nodes:
                if not node.value or not node.value.strip():
                    continue
                cursor = await db.execute("""
                    INSERT INTO graph_nodes (node_key, node_type, value, canonical_value, metadata, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(node_key) DO UPDATE SET updated_at = excluded.updated_at
                """, (node.node_key, node.node_type, node.value, node.canonical_value,
                      json.dumps(node.metadata), now, now))
                if node.node_key not in key_map:
                    key_map[node.node_key] = cursor.lastrowid
            await db.commit()
        return key_map

    async def add_edge(self, edge: GraphEdge, node_key_to_id: dict[str, int]) -> int | None:
        src_id = node_key_to_id.get(edge.source_key)
        tgt_id = node_key_to_id.get(edge.target_key)
        if not src_id or not tgt_id:
            return None
        now = self._now_iso()
        async with self._connect() as db:
            cursor = await db.execute("""
                INSERT INTO graph_edges
                (edge_key, semantic_key, source_node_id, target_node_id, relation_type,
                 source_memory_id, weight, confidence, status, metadata, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', '{}', ?, ?)
                ON CONFLICT(edge_key) DO UPDATE SET weight = weight + 0.1, updated_at = excluded.updated_at
            """, (edge.edge_key, edge.semantic_key, src_id, tgt_id, edge.relation_type,
                  edge.source_memory_id, edge.weight, edge.confidence, now, now))
            await db.commit()
            return cursor.lastrowid

    async def add_edge_by_ids(
        self,
        edge_key: str,
        source_node_id: int,
        target_node_id: int,
        relation_type: str,
        source_memory_id: int,
        weight: float = 1.0,
        confidence: float = 0.8,
    ) -> int | None:
        """按节点 ID 直接添加边（不通过 GraphEdge 对象）"""
        now = self._now_iso()
        async with self._connect() as db:
            cursor = await db.execute("""
                INSERT INTO graph_edges
                (edge_key, semantic_key, source_node_id, target_node_id, relation_type,
                 source_memory_id, weight, confidence, status, metadata, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', '{}', ?, ?)
                ON CONFLICT(edge_key) DO UPDATE SET weight = weight + 0.1, updated_at = excluded.updated_at
            """, (
                edge_key, f"{relation_type}:{source_node_id}:{target_node_id}",
                source_node_id, target_node_id, relation_type,
                source_memory_id, weight, confidence, now, now,
            ))
            await db.commit()
            return cursor.lastrowid

    async def update_cooccur(self, entity_ids: list[int]):
        """批量更新实体共现计数"""
        if len(entity_ids) < 2:
            return
        now = self._now_iso()
        async with self._connect() as db:
            for i in range(len(entity_ids)):
                for j in range(i + 1, len(entity_ids)):
                    a, b = entity_ids[i], entity_ids[j]
                    if a == b:
                        continue
                    key = f"{min(a,b)}:{max(a,b)}"
                    await db.execute("""
                        INSERT INTO entity_cooccur (entity_a_id, entity_b_id, count, last_updated)
                        VALUES (?, ?, 1, ?)
                        ON CONFLICT(entity_a_id, entity_b_id)
                        DO UPDATE SET count = count + 1, last_updated = excluded.last_updated
                    """, (min(a, b), max(a, b), now))
            await db.commit()

    async def get_cooccurring(self, entity_id: int, k: int = 10) -> list[dict]:
        """获取与某实体最常共现的实体"""
        rows = await self.fetch("""
            SELECT e.id, e.value, e.node_type, ec.count
            FROM entity_cooccur ec
            JOIN graph_nodes e ON e.id = CASE WHEN ec.entity_a_id = ? THEN ec.entity_b_id ELSE ec.entity_a_id END
            WHERE (ec.entity_a_id = ? OR ec.entity_b_id = ?)
            ORDER BY ec.count DESC LIMIT ?
        """, (entity_id, entity_id, entity_id, k))
        return [{"id": r[0], "label": r[1], "type": r[2], "count": r[3]} for r in rows]

    async def get_neighbors(self, entity_name: str, k: int = 20) -> list[dict]:
        """获取实体的 k=1 邻居（实体→日记→其他实体）"""
        cv = entity_name.strip().lower().replace(" ", "_")[:80]
        rows = await self.fetch("""
            SELECT DISTINCT e2.id, e2.value, e2.node_type, ge2.relation_type, ge2.weight
            FROM graph_edges ge1
            JOIN graph_nodes n1 ON ge1.source_node_id = n1.id
            JOIN graph_edges ge2 ON ge1.source_memory_id = ge2.source_memory_id AND ge2.id != ge1.id
            JOIN graph_nodes e2 ON ge2.source_node_id = e2.id
            WHERE n1.canonical_value = ? AND n1.node_type IN ('entity', 'user')
            AND e2.node_type IN ('entity', 'user')
            LIMIT ?
        """, (cv, k))
        return [{"id": r[0], "label": r[1], "type": r[2], "relation": r[3], "weight": r[4]} for r in rows]

    async def delete_memory_edges(self, source_memory_id: int):
        async with self._connect() as db:
            await db.execute("DELETE FROM graph_edges WHERE source_memory_id = ?", (source_memory_id,))
            await db.commit()

    async def get_graph_overview(self) -> dict:
        """图谱概览统计"""
        async with self._connect() as db:
            total_nodes = (await db.execute_fetchall("SELECT COUNT(*) FROM graph_nodes"))[0][0]
            total_edges = (await db.execute_fetchall("SELECT COUNT(*) FROM graph_edges WHERE status='active'"))[0][0]
            by_type = await db.execute_fetchall("""
                SELECT node_type, COUNT(*) FROM graph_nodes GROUP BY node_type ORDER BY COUNT(*) DESC
            """)
            top_nodes = await db.execute_fetchall("""
                SELECT n.node_type, n.value, n.canonical_value,
                       CAST(json_extract(n.metadata, '$.count') AS INTEGER) as ref_count,
                       (SELECT COUNT(*) FROM graph_edges e
                        WHERE (e.source_node_id = n.id OR e.target_node_id = n.id) AND e.status='active') as degree
                FROM graph_nodes n
                ORDER BY ref_count DESC LIMIT 30
            """)
        return {
            "total_nodes": total_nodes,
            "total_edges": total_edges,
            "by_type": dict(by_type),
            "top_nodes": [
                {"type": r[0], "value": r[1], "canonical": r[2], "refs": r[3] or 0, "degree": r[4] or 0}
                for r in top_nodes
            ],
        }

    async def query_graph(self, query: str, limit: int = 50) -> dict:
        """搜索图谱，返回关联的子图"""
        async with self._connect() as db:
            # 搜索节点
            nodes = await db.execute_fetchall("""
                SELECT id, node_type, value, canonical_value,
                       CAST(json_extract(metadata, '$.count') AS INTEGER) as ref_count
                FROM graph_nodes
                WHERE value LIKE ? OR canonical_value LIKE ?
                ORDER BY ref_count DESC LIMIT 20
            """, (f"%{query}%", f"%{query}%"))

            node_ids = [r[0] for r in nodes]
            edges = []
            if node_ids:
                placeholders = ",".join("?" for _ in node_ids)
                edges = await db.execute_fetchall(f"""
                    SELECT e.id, e.source_node_id, e.target_node_id, e.relation_type,
                           e.weight, sn.value as sv, tn.value as tv
                    FROM graph_edges e
                    JOIN graph_nodes sn ON e.source_node_id = sn.id
                    JOIN graph_nodes tn ON e.target_node_id = tn.id
                    WHERE (e.source_node_id IN ({placeholders}) OR e.target_node_id IN ({placeholders}))
                    AND e.status = 'active'
                    ORDER BY e.weight DESC LIMIT {limit}
                """, node_ids + node_ids)

        return {
            "nodes": [
                {"id": r[0], "type": r[1], "label": r[2], "canonical": r[3], "refs": r[4] or 0}
                for r in nodes
            ],
            "edges": [
                {"id": r[0], "source": r[1], "target": r[2], "relation": r[3], "weight": r[4],
                 "source_label": r[5], "target_label": r[6]}
                for r in edges
            ],
        }

    async def get_full_graph(self, limit: int = 200) -> dict:
        """获取全量图谱数据（供 WebUI 渲染）"""
        async with self._connect() as db:
            nodes = await db.execute_fetchall("""
                SELECT id, node_type, value, canonical_value,
                       CAST(json_extract(metadata, '$.count') AS INTEGER) as ref_count,
                       (SELECT COUNT(*) FROM graph_edges e
                        WHERE (e.source_node_id = n.id OR e.target_node_id = n.id) AND e.status='active') as degree
                FROM graph_nodes n
                ORDER BY ref_count DESC LIMIT ?
            """, (limit,))

            node_ids = [r[0] for r in nodes]
            edges = []
            if node_ids:
                placeholders = ",".join("?" for _ in node_ids)
                edges = await db.execute_fetchall(f"""
                    SELECT e.id, e.source_node_id, e.target_node_id, e.relation_type, e.weight,
                           sn.node_type as st, tn.node_type as tt
                    FROM graph_edges e
                    JOIN graph_nodes sn ON e.source_node_id = sn.id
                    JOIN graph_nodes tn ON e.target_node_id = tn.id
                    WHERE e.source_node_id IN ({placeholders})
                    AND e.target_node_id IN ({placeholders})
                    AND e.status = 'active'
                    ORDER BY e.weight DESC
                """, node_ids + node_ids)

        return {
            "nodes": [
                {"id": r[0], "type": r[1], "label": r[2], "canonical": r[3],
                 "refs": r[4] or 0, "degree": r[5] or 0}
                for r in nodes
            ],
            "edges": [
                {"id": r[0], "source": r[1], "target": r[2], "relation": r[3],
                 "weight": r[4], "source_type": r[5], "target_type": r[6]}
                for r in edges
            ],
        }

    async def update_node_embedding(self, node_id: int, embedding: list[float], model_name: str):
        """写入单条节点的 embedding 向量"""
        blob = json.dumps(embedding).encode("utf-8")
        await self.execute(
            "UPDATE graph_nodes SET embedding=?, embedding_model=? WHERE id=?",
            (blob, model_name, node_id),
        )

    async def search_vector(
        self,
        query_embed: list[float],
        k: int = 10,
        model_name: str | None = None,
    ) -> list[tuple[int, float]]:
        """向量搜索节点：余弦相似度排序

        Args:
            query_embed: 查询向量
            k: 返回 top N
            model_name: 筛选指定模型生成的 embedding

        Returns:
            [(node_id, cosine_similarity), ...]
        """
        if not query_embed:
            return []

        # 加载有 embedding 的节点
        model_filter = "AND embedding_model=?" if model_name else ""
        try:
            rows = await self.fetch(
                f"SELECT id, embedding FROM graph_nodes "
                f"WHERE embedding IS NOT NULL {model_filter} AND node_type IN ('entity','topic','user') "
                f"ORDER BY updated_at DESC LIMIT 500",
                (model_name,) if model_name else (),
            )
        except Exception:
            return []

        if not rows:
            return []

        q_norm = sum(x * x for x in query_embed) ** 0.5
        if q_norm < 1e-9:
            return []

        scored: list[tuple[int, float]] = []
        for nid, blob in rows:
            if not blob:
                continue
            try:
                stored = json.loads(blob.decode("utf-8"))
            except Exception:
                continue
            if not stored or len(stored) != len(query_embed):
                continue
            dot = sum(a * b for a, b in zip(query_embed, stored))
            n_norm = sum(x * x for x in stored) ** 0.5
            if n_norm < 1e-9:
                continue
            cos_sim = dot / (q_norm * n_norm)
            scored.append((nid, max(0.0, cos_sim)))

        scored.sort(key=lambda x: -x[1])
        return scored[:k]

    async def search_fts(self, query: str, k: int = 10) -> list[dict]:
        """在 graph_nodes 上全文搜索

        英文/数字走 FTS5 MATCH，中文走 LIKE %kw%。与 BM25Retriever 双模策略一致。
        """
        if not query or not query.strip():
            return []
        import re

        cleaned = re.sub(r'[^\w一-鿿]', ' ', query).strip()
        if not cleaned:
            return []
        terms = cleaned.split()

        ascii_terms = [t for t in terms if t.isascii() and len(t) >= 2]
        cjk_terms = [t for t in terms if not t.isascii() and len(t) >= 2]

        results: dict[int, dict] = {}

        # FTS5 MATCH（英文/数字）
        if ascii_terms:
            try:
                fts_q = " OR ".join(f'"{t}"*' for t in ascii_terms)
                rows = await self.fetch(
                    """SELECT n.id, n.node_type, n.value, n.canonical_value, rank
                       FROM graph_nodes_fts
                       JOIN graph_nodes n ON graph_nodes_fts.rowid = n.id
                       WHERE graph_nodes_fts MATCH ?
                       ORDER BY rank
                       LIMIT ?""",
                    (fts_q, k * 2),
                )
                for r in rows:
                    results[r[0]] = {
                        "id": r[0], "node_type": r[1], "value": r[2],
                        "canonical_value": r[3], "score": 1.0,
                    }
            except Exception:
                pass

        # LIKE 查询（中文）
        if cjk_terms:
            for term in cjk_terms:
                try:
                    rows = await self.fetch(
                        """SELECT id, node_type, value, canonical_value
                           FROM graph_nodes
                           WHERE (value LIKE ? OR canonical_value LIKE ?)
                             AND node_type IN ('entity','topic','user')
                           LIMIT 20""",
                        (f"%{term}%", f"%{term}%"),
                    )
                    for r in rows:
                        if r[0] not in results:
                            results[r[0]] = {
                                "id": r[0], "node_type": r[1], "value": r[2],
                                "canonical_value": r[3], "score": 0.8,
                            }
                except Exception:
                    pass

        return list(results.values())[:k]

    @staticmethod
    def _now_iso() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()
