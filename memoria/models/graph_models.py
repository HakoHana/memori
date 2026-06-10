"""图谱数据模型"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class GraphNode:
    """图谱节点 — 话题/实体/类型"""
    node_type: str          # topic / entity / atom_type
    value: str              # 显示值
    canonical_value: str    # 归一化值（用于去重）
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def node_key(self) -> str:
        return f"{self.node_type}:{self.canonical_value}"


@dataclass(slots=True)
class GraphEdge:
    """图谱边 — 节点间的关联"""
    source_key: str
    target_key: str
    relation_type: str      # co_occur / same_diary / same_topic
    source_memory_id: int   # 关联的原子 ID
    weight: float = 1.0     # 权重
    confidence: float = 0.8

    @property
    def edge_key(self) -> str:
        return f"{self.source_key}|{self.relation_type}|{self.target_key}|{self.source_memory_id}"

    @property
    def semantic_key(self) -> str:
        """忽略 memory_id 的聚合键（用于合并同类型边）"""
        return f"{self.source_key}|{self.relation_type}|{self.target_key}"


@dataclass(slots=True)
class ExtractedGraph:
    """一次提取产生的图谱快照"""
    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
