"""图谱存储 — SQLite 实现"""

from __future__ import annotations

import json
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

    @staticmethod
    def _now_iso() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()
