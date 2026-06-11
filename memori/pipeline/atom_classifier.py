"""规则基原子分类器 — 无需额外 LLM 调用

从关键事实（key_facts）中通过正则模式匹配自动分类原子类型、
计算置信度、解析事件时间，并生成 MemoryAtom 对象。

用法:
    from .atom_classifier import classify_atoms
    atoms = classify_atoms(key_facts, entities=["Hako"], parent_importance=0.8)
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from typing import Any

from ..models.memory_atom import (
    MemoryAtom,
    AtomType,
    AtomStatus,
    DecayType,
    compute_expires_at,
)

# ── 中文时间指示词 ──
_TIME_INDICATORS = re.compile(
    r"明天|后天|大后天|昨天|前天|今天|"
    r"(?:上周|本周|下下周|下周)?周[一二三四五六日天]|"
    r"上周|本周|下下周|下周|"
    r"下个?月|上个?月|明年|后年|去年|前年|"
    r"\d{1,2}月\d{1,2}[日号]|\d{4}年\d{1,2}月|"
    r"上午|下午|晚上|凌晨|早上|中午|傍晚|"
    r"\d{1,2}[点时：:]\d{1,2}"
)

# 过去时间指示词（标志已发生的事件 → EPISODIC）
_PAST_TIME_INDICATORS = re.compile(
    r"昨天|前天|刚才|刚刚|之前|以前|过去|"
    r"上周|上个?月|去年|前年|前阵子|前些天"
)

# 未来时间指示词（标志计划/约定 → PLANNED）
_FUTURE_TIME_INDICATORS = re.compile(
    r"明天|后天|大后天|下下周|下周|下个?月|明年|后年|"
    r"今晚|今晚[上]|待会|一会儿|等会"
)

# ── 行动动词（标志事件/计划） ──
_ACTION_VERBS = re.compile(
    r"开会|讨论|参加|组织|安排|举办|进行|执行|完成|提交|发送|发布|"
    r"去|来|到|做|要|准备|计划|打算|说|告诉|问|回答|"
    r"买|卖|给|送|取|订|约|见面|碰头|出发|出发|启程|到达"
)

# ── 状态性描述（标志事实型） ──
_STATIVE_PATTERNS = re.compile(r"是|有|属于|等于|代表|意味|包含|包括|位于|住[在]|住")

# ── 关系关键词 ──
_RELATION_PATTERNS = re.compile(
    r"同事|朋友|同学|家人|亲戚|队友|搭档|伙伴|老板|上司|下属|"
    r"合作|合伙|夫妻|情侣|邻居|室友|老乡|兄弟|姐妹|"
    r"闺蜜|基友|死党"
)

# ── 偏好关键词 ──
_PREFERENCE_PATTERNS = re.compile(
    r"喜欢|讨厌|爱|不爱|偏好|最爱|不喜欢|热衷于|沉迷|"
    r"爱吃|爱喝|喜欢喝|喜欢去|讨厌吃|讨厌去|"
    r"觉得好|不错|很棒|超爱|很喜欢"
)

# ── 人名词典辅助 ──
_WEEKDAY_INDEX = {
    "一": 0, "二": 1, "三": 2, "四": 3,
    "五": 4, "六": 5, "日": 6, "天": 6,
}


def _parse_weekday_time(text: str, now: float) -> float | None:
    """解析中文星期表达为绝对时间戳"""
    match = re.search(r"(上周|本周|下下周|下周)?周([一二三四五六日天])", text)
    if not match:
        return None
    prefix = match.group(1) or ""
    target_weekday = _WEEKDAY_INDEX[match.group(2)]
    now_dt = datetime.fromtimestamp(now)

    if prefix == "上周":
        days_delta = target_weekday - now_dt.weekday() - 7
    elif prefix == "本周":
        days_delta = target_weekday - now_dt.weekday()
    elif prefix == "下周":
        days_delta = target_weekday - now_dt.weekday() + 7
    elif prefix == "下下周":
        days_delta = target_weekday - now_dt.weekday() + 14
    else:
        days_delta = (target_weekday - now_dt.weekday()) % 7

    return (now_dt + timedelta(days=days_delta)).timestamp()


def _parse_event_time(text: str) -> float | None:
    """从文本中尽力提取绝对时间戳（中文时间表达转时间戳）"""
    now = time.time()
    day_sec = 86400.0

    # 绝对时间词
    mapping: dict[str, float] = {
        "前天": -2 * day_sec,
        "昨天": -1 * day_sec,
        "今天": 0,
        "明天": 1 * day_sec,
        "后天": 2 * day_sec,
        "大后天": 3 * day_sec,
    }
    for word, offset in mapping.items():
        if word in text:
            return now + offset

    weekday_time = _parse_weekday_time(text, now)
    if weekday_time is not None:
        return weekday_time

    week_mapping: dict[str, float] = {
        "上周": -7 * day_sec,
        "本周": 0,
        "下下周": 14 * day_sec,
        "下周": 7 * day_sec,
    }
    for word, offset in week_mapping.items():
        if word in text:
            return now + offset

    # 月日格式 "5月30日"
    m = re.search(r"(\d{1,2})月(\d{1,2})[日号]", text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        now_dt = datetime.fromtimestamp(now)
        try:
            target = now_dt.replace(
                month=month, day=day, hour=0, minute=0, second=0, microsecond=0
            )
            if target < now_dt:
                target = target.replace(year=now_dt.year + 1)
            return target.timestamp()
        except (ValueError, OverflowError):
            pass

    return None


def _classify_single(text: str) -> tuple[AtomType, float, float | None]:
    """分类单条关键事实，返回 (类型, 置信度, 事件时间戳)"""
    has_time = bool(_TIME_INDICATORS.search(text))
    has_action = bool(_ACTION_VERBS.search(text))
    has_stative = bool(_STATIVE_PATTERNS.search(text))
    has_relation = bool(_RELATION_PATTERNS.search(text))
    has_preference = bool(_PREFERENCE_PATTERNS.search(text))
    has_past_time = bool(_PAST_TIME_INDICATORS.search(text))
    has_future_time = bool(_FUTURE_TIME_INDICATORS.search(text))

    event_time = _parse_event_time(text) if has_time else None

    # PLANNED: 未来时间词 + 行动动词 → 计划
    if has_future_time and has_action:
        return AtomType.PLANNED, 0.85, event_time

    # EPISODIC: 过去时间词 + 行动动词 → 已发生事件
    if has_past_time and has_action:
        return AtomType.EPISODIC, 0.80, event_time

    # PREFERENCE: 偏好关键词优先
    if has_preference:
        return AtomType.PREFERENCE, 0.82, None

    # RELATIONAL: 关系词
    if has_relation:
        return AtomType.RELATIONAL, 0.80, None

    # FACTUAL: 状态性描述
    if has_stative:
        return AtomType.FACTUAL, 0.78, None

    # 有时间+行动但无法区分过去/未来 → 如果是"今天"默认为 EPISODIC
    if has_time and has_action:
        return AtomType.EPISODIC, 0.75, event_time

    # EPISODIC: 有行动动词但无时间
    if has_action:
        return AtomType.EPISODIC, 0.75, None

    # 无匹配 → 默认 factual
    return AtomType.FACTUAL, 0.65, None


def classify_atoms(
    key_facts: list[str],
    entities: list[str] | None = None,
    parent_importance: float = 0.5,
    user_id: str = "",
    diary_date: str = "",
) -> list[MemoryAtom]:
    """将关键事实列表分类为 MemoryAtom 实例

    Args:
        key_facts: 从 LLM 响应中提取的原始事实字符串
        entities: 实体列表（从对话参与者/主题推断）
        parent_importance: 从父记忆中继承的重要性
        user_id: 用户标识
        diary_date: 日记日期 (YYYY-MM-DD)

    Returns:
        分类后的 MemoryAtom 列表，每个原子包含计算好的
        atom_type / confidence / expires_at / decay_type
    """
    entities = entities or []
    now = time.time()
    atoms: list[MemoryAtom] = []

    for fact in key_facts:
        fact = fact.strip()
        if not fact or len(fact) < 4:
            continue

        atom_type, confidence, event_time = _classify_single(fact)

        atom = MemoryAtom(
            user_id=user_id,
            diary_date=diary_date,
            content=fact,
            atom_type=atom_type,
            entities=list(entities),
            importance=parent_importance,
            confidence=confidence,
        )
        # 如果有解析到事件时间，存入 metadata
        if event_time is not None:
            atom.metadata["event_time"] = event_time

        # 计算过期时间和衰减类型
        atom.prepare_insert()
        atoms.append(atom)

    return atoms


__all__ = ["classify_atoms", "_classify_single", "_parse_event_time"]
