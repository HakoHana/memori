"""消息时间格式化 — 给 LLM 的消息加上谁+什么时间+做了什么"""

from __future__ import annotations

import time
from datetime import datetime


def format_msg(
    timestamp: float,
    display_name: str,
    content: str,
    now: float | None = None,
    with_seconds: bool = True,
) -> str:
    """格式化一条消息为 LLM 友好的带时间格式

    规则：
      - 当天消息 → [HH:MM] 昵称: 内容
      - 跨天消息（≤7天）→ [MM-DD HH:MM] 昵称: 内容
      - 超过7天   → [MM-DD] 昵称: 内容（省略具体时间）

    Args:
        timestamp: Unix 秒时间戳
        display_name: 显示昵称（对 Bot 已含 "Bot:" 前缀）
        content: 消息原文
        now: 参考时间（默认当前时间）
        with_seconds: 是否显示分钟（默认 True，显示 HH:MM）

    Returns:
        格式化后的字符串
    """
    if now is None:
        now = time.time()

    dt = datetime.fromtimestamp(timestamp)
    ref = datetime.fromtimestamp(now)
    td = ref.date() - dt.date()

    if td.days == 0:
        # 当天 → [HH:MM]
        time_str = dt.strftime("%H:%M")
    elif td.days < 7:
        # 跨天但 ≤7天 → [MM-DD HH:MM]
        time_str = dt.strftime("%m-%d %H:%M")
    else:
        # 超过7天 → [MM-DD]
        time_str = dt.strftime("%m-%d")

    return f"[{time_str}] {display_name}: {content}"


def format_date_tag(date_str: str, now: float | None = None) -> str:
    """格式化日期标签（用于召回的记忆原子，只有日期没有精确时间）

    规则：
      - 当天 → 今日
      - 昨天 → 昨天
      - ≤7天 → MM-DD
      - >7天 → MM-DD（同上，或可自定义前缀）

    Args:
        date_str: YYYY-MM-DD 格式日期
        now: 参考时间戳

    Returns:
        格式化的短日期标签
    """
    if not date_str:
        return ""

    if now is None:
        now = time.time()

    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return date_str

    ref = datetime.fromtimestamp(now)
    td = ref.date() - dt.date()

    if td.days == 0:
        return "今日"
    elif td.days == 1:
        return "昨天"
    else:
        return dt.strftime("%m-%d")
