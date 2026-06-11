"""记忆输出质量校验层 — 轻量可组合的校验函数

检测泛化词、空摘要、低重要性等质量问题。
低质量结果仍会存储，但会记录警告。
"""

from __future__ import annotations

import re
from typing import Any

# 泛化词列表：这些词出现在 summary/diary 中说明 LLM 没有使用真实昵称
_GENERIC_TERMS = [
    "某用户", "有人", "某人",
    "用户说", "对方说", "群成员", "某群成员",
    "该用户", "一方", "另一方",
    "someone", "somebody", "the user",
]

# 中文人名常用姓氏（用于检测是否使用了真实人名）
_CHINESE_SURNAMES = set(
    "张李王刘陈杨赵黄周吴徐孙胡朱高何"
    "林马郭罗梁宋郑谢韩唐冯于董萧"
    "程曹袁邓许傅沈曾彭吕苏卢蒋蔡"
    "贾丁魏薛叶阎余潘杜戴夏钟汪田"
    "任姜范方石姚谭廖邹熊金陆郝孔"
)


def validate_diary_quality(diary: str, min_length: int = 10) -> str:
    """校验日记质量

    Returns:
        "normal" | "low"
    """
    if not diary or len(diary.strip()) < min_length:
        return "low"
    return "normal"


def validate_atoms_quality(atoms: list, min_count: int = 1) -> str:
    """校验原子质量

    Args:
        atoms: MemoryAtom 对象列表或 dict 列表
        min_count: 最少原子数

    Returns:
        "normal" | "low"
    """
    if not atoms or len(atoms) < min_count:
        return "low"
    # 检查是否有有意义的 content
    valid = 0
    for a in atoms:
        content = ""
        if isinstance(a, dict):
            content = a.get("content", "")
        elif hasattr(a, "content"):
            content = a.content
        if content and len(str(content).strip()) >= 4:
            valid += 1
    return "normal" if valid >= min_count else "low"


def has_generic_terms(text: str) -> bool:
    """检查文本中是否包含泛化词（「用户」「对方」等）"""
    if not text:
        return False
    text_lower = text.lower()
    for term in _GENERIC_TERMS:
        if term in text_lower or term in text:
            return True
    return False


def has_real_name(text: str) -> bool:
    """检查文本中是否包含疑似中文人名的词"""
    if not text:
        return False
    # 检测 "姓+名" 模式（姓氏 + 1-2 个汉字）
    # Python 3.14 re 要求 look-behind 固定宽度，拆分 ^ 和字符类
    return bool(re.search(
        rf"(?:^|(?<=[，。！？、\s\[【]))[{''.join(_CHINESE_SURNAMES)}][一-鿿]{{1,3}}"
        rf"(?=说|表示|提到|告诉|问|回答|来了|去了|在|是|有|的|$)",
        text,
    ))


def validate_merged_output(
    diary: str,
    atoms: list[Any],
    importance: float = 0.5,
) -> dict[str, str]:
    """校验合并调用输出的整体质量

    Returns:
        {"diary": "normal"|"low", "atoms": "normal"|"low",
         "importance": "normal"|"low", "generic_terms": bool}
    """
    result: dict[str, str] = {
        "diary": "normal",
        "atoms": "normal",
        "importance": "normal",
        "generic_terms": False,
    }

    # 日记
    if not diary or len(diary.strip()) < 10:
        result["diary"] = "low"

    # 原子
    if not atoms or len(atoms) < 1:
        result["atoms"] = "low"

    # 重要性
    if not isinstance(importance, (int, float)) or not (0.0 <= importance <= 1.0):
        result["importance"] = "low"

    # 泛化词检测
    if diary and has_generic_terms(diary):
        result["generic_terms"] = True

    return result


__all__ = [
    "validate_diary_quality",
    "validate_atoms_quality",
    "has_generic_terms",
    "has_real_name",
    "validate_merged_output",
]
