"""记忆插件数据模型"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AtomType(str, Enum):
    """原子记忆类型"""
    EPISODIC = "episodic"       # 情景/事件
    FACTUAL = "factual"         # 事实型
    PREFERENCE = "preference"   # 偏好型
    PLANNED = "planned"         # 计划型
    RELATIONAL = "relational"   # 关系型
    UNKNOWN = "unknown"


class AtomStatus(str, Enum):
    """原子生命周期状态"""
    ACTIVE = "active"           # 活跃
    DORMANT = "dormant"         # 休眠
    ARCHIVED = "archived"       # 归档
    FORGOTTEN = "forgotten"     # 遗忘


class DecayType(str, Enum):
    """衰减类型"""
    EXPONENTIAL = "exponential"  # 指数衰减（默认）
    LINEAR = "linear"            # 线性衰减
    STEP = "step"                # 阶梯衰减（到 TTL 直接降为 0）


# 每种原子类型的默认 TTL 和衰减类型
_ATOM_TTL_CONFIG = {
    AtomType.EPISODIC:   {"ttl_days": 30,  "decay": DecayType.EXPONENTIAL},
    AtomType.FACTUAL:    {"ttl_days": 180, "decay": DecayType.EXPONENTIAL},
    AtomType.PREFERENCE: {"ttl_days": 60,  "decay": DecayType.EXPONENTIAL},
    AtomType.PLANNED:    {"ttl_days": 7,   "decay": DecayType.STEP},
    AtomType.RELATIONAL: {"ttl_days": 90,  "decay": DecayType.LINEAR},
    AtomType.UNKNOWN:    {"ttl_days": 30,  "decay": DecayType.EXPONENTIAL},
}


def compute_expires_at(atom_type: AtomType, importance: float, ttl_days: float = 0) -> tuple[float, DecayType]:
    """计算过期时间和衰减类型

    Returns:
        (expires_at, decay_type)
    """
    cfg = _ATOM_TTL_CONFIG.get(atom_type, _ATOM_TTL_CONFIG[AtomType.UNKNOWN])

    # 基础 TTL
    base_ttl = cfg["ttl_days"] if ttl_days <= 0 else ttl_days

    # 重要度因子：重要的事情 TTL 更长
    importance_factor = 0.5 + max(0.0, min(1.0, importance))
    actual_ttl = base_ttl * importance_factor

    expires_at = time.time() + actual_ttl * 86400
    return expires_at, cfg["decay"]


def compute_decay_score(
    decay_type: DecayType, ttl_days: float, age_days: float
) -> float:
    """计算衰减分数 (0~1)，用于热温冷分层"""
    effective_ttl = max(1.0, ttl_days)
    age_days = max(0.0, age_days)

    if decay_type == DecayType.LINEAR:
        return max(0.0, 1.0 - age_days / effective_ttl)
    if decay_type == DecayType.STEP:
        return 1.0 if age_days <= effective_ttl else 0.05

    # EXPONENTIAL: 半衰期 = TTL/2
    half_life = effective_ttl / 2.0
    return math.exp(-math.log(2) * age_days / max(0.5, half_life))


@dataclass(slots=True)
class MemoryAtom:
    """记忆原子 — 结构化事实的最小单元"""
    user_id: str
    diary_date: str                    # YYYY-MM-DD
    content: str = ""
    atom_type: AtomType = AtomType.UNKNOWN
    entities: list[str] = field(default_factory=list)
    importance: float = 0.5            # 0.0 ~ 1.0
    confidence: float = 0.7            # 0.0 ~ 1.0
    access_count: int = 0
    created_at: float = field(default_factory=time.time)
    last_accessed_at: float | None = None
    ttl_days: float = 30.0
    expires_at: float = 0.0       # 过期时间戳（插入时自动计算）
    decay_type: DecayType = DecayType.EXPONENTIAL  # 衰减类型
    status: AtomStatus = AtomStatus.ACTIVE
    session_id: str | None = None
    diary_ref: str | None = None
    diary_snippet: str = ""          # 日记原文片段（溯源用，不参与检索/注入）
    diary_id: int = 0                  # 关联的日记条目 ID（用于精确关联）
    embedding: list[float] | None = None  # 向量（由 EmbeddingProvider 计算，BLOB 存储）
    metadata: dict[str, Any] = field(default_factory=dict)
    atom_id: int = 0                   # 数据库 ID，插入后填充

    def prepare_insert(self):
        """插入前准备：计算 expires_at 和 decay_type"""
        self.expires_at, self.decay_type = compute_expires_at(
            self.atom_type, self.importance, self.ttl_days
        )

    @property
    def is_expired(self) -> bool:
        """检查是否超过过期时间"""
        if self.expires_at <= 0:
            return False
        return time.time() >= self.expires_at

    @property
    def decay_score(self) -> float:
        """当前衰减分数 (0~1)"""
        age_days = (time.time() - self.created_at) / 86400
        return compute_decay_score(self.decay_type, self.ttl_days, age_days)


@dataclass(slots=True)
class CaptureJudgeResult:
    """LLM 判断结果 — 值不值得记"""
    should_remember: bool = False
    reason: str = ""
    importance: float = 0.0
    mood: str = ""
    context_summary: str = ""


@dataclass(slots=True)
class CaptureResult:
    """一次抓取的结果"""
    wrote_diary: bool = False
    diary_content: str = ""
    atoms: list[MemoryAtom] = field(default_factory=list)

    @property
    def atom_count(self) -> int:
        return len(self.atoms)


@dataclass(slots=True)
class PersistedSessionState:
    """持久化的会话状态 — 每个用户一份"""
    user_id: str
    msg_count: int = 0
    warmup_threshold: int = 3          # 初始暖启动阈值（第3条消息才触发首次整理）
    last_consolidated_at: float = 0.0
    last_diary_date: str = ""
    diary_count: int = 0
    diary_count_since_persona: int = 0
    l1_retry_count: int = 0

    def reset_after_consolidation(self):
        """整理后重置计数"""
        self.msg_count = 0
        self.l1_retry_count = 0
        self.last_consolidated_at = time.time()
        self.diary_count += 1
        self.diary_count_since_persona += 1


@dataclass(slots=True)
class RecallResult:
    """召回结果 — 供注入器使用"""
    memory_text: str = ""
    atoms: list[MemoryAtom] = field(default_factory=list)
    persona_text: str | None = None
    diary_refs: list[dict] = field(default_factory=list)
    """原子关联的日记溯源片段列表: [{diary_id, date, snippet}, ...]"""
