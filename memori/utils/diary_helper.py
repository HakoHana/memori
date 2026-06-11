"""日记内容格式化工具 — frontmatter + Markdown 双模存储

设计目标：
- content 字段同时包含结构化元数据（frontmatter）和可读正文（Markdown）
- SQL 各字段（topics, sentiment, importance 等）作为冗余索引，保持同步
- 任何时候 content 是最完整的表达，导出即 Obsidian 兼容笔记
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import Any


# ── Frontmatter 解析 ───────────────────────────────────────────

def parse_diary_content(content: str) -> tuple[dict[str, Any], str]:
    """解析日记内容，返回 (frontmatter_dict, body_markdown)

    支持容错：frontmatter 格式错误时返回空 dict 和完整原文。
    """
    if not content or not content.startswith("---"):
        return {}, (content or "")

    # 查找 closing ---
    end = content.find("\n---", 3)
    if end == -1:
        return {}, content

    raw = content[3:end].strip()
    body = content[end + 4:].strip()

    front = _parse_yaml_like(raw)
    return front, body


def _parse_yaml_like(raw: str) -> dict[str, Any]:
    """简易 YAML 解析（只处理 frontmatter 常见的标量 + 列表）"""
    result: dict[str, Any] = {}
    current_list_key: str | None = None

    for line in raw.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue

        # 列表项: - value
        list_match = re.match(r"^\s*-\s+(.+)$", line)
        if list_match and current_list_key:
            val = _parse_yaml_value(list_match.group(1))
            if isinstance(result.setdefault(current_list_key, []), list):
                result[current_list_key].append(val)
            continue

        current_list_key = None
        # key: value
        kv_match = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*(.*)$", line)
        if kv_match:
            key = kv_match.group(1)
            val_raw = kv_match.group(2).strip()
            if val_raw == "" or val_raw.startswith("#"):
                # 可能是列表开始，先不设值
                current_list_key = key
                if key not in result:
                    result[key] = []
                continue
            result[key] = _parse_yaml_value(val_raw)

    return result


def _parse_yaml_value(raw: str) -> Any:
    """解析 YAML 标量值"""
    v = raw.strip()
    if not v:
        return ""

    # 引号字符串
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        return v[1:-1]

    # 布尔值
    if v.lower() in ("true", "yes"):
        return True
    if v.lower() in ("false", "no"):
        return False

    # 数字
    try:
        if "." in v:
            return float(v)
        return int(v)
    except (ValueError, TypeError):
        pass

    # JSON 数组（如 ["factual", "episodic"] 或用户输入的简化格式）
    if v.startswith("[") and v.endswith("]"):
        # 尝试 strict JSON
        try:
            return json.loads(v)
        except (json.JSONDecodeError, TypeError):
            pass
        # 容错：未加引号的字符串元素
        try:
            cleaned = re.sub(r'(?<=[,\[])\s*([a-zA-Z_一-鿿][a-zA-Z0-9_一-鿿]*)\s*(?=[,\]])', r'"\1"', v)
            return json.loads(cleaned)
        except (json.JSONDecodeError, TypeError):
            pass

    return v


# ── Frontmatter 构建 ───────────────────────────────────────────

def build_diary_content(
    frontmatter: dict[str, Any] | None = None,
    body: str = "",
    *,
    diary_entry: Any = None,
) -> str:
    """构建完整日记内容 = YAML frontmatter + Markdown 正文

    如果传入了 diary_entry（sqlite3.Row 或 dict），自动提取字段。
    未提供的字段使用默认值。
    """
    fm: dict[str, Any] = {}

    # 用现有 frontmatter 覆盖默认值
    if frontmatter:
        fm = {k: v for k, v in frontmatter.items() if v is not None and v != ""}

    # 如果提供了 DB 行，用行数据增强
    if diary_entry is not None:
        row = dict(diary_entry) if not isinstance(diary_entry, dict) else diary_entry
        fm.setdefault("date", row.get("date", ""))
        fm.setdefault("importance", row.get("importance", 0.5))
        fm.setdefault("mood", _sentiment_to_mood(row.get("sentiment", "")))
        topics = row.get("topics", "")
        if topics:
            if isinstance(topics, str):
                try:
                    topics = json.loads(topics)
                except (json.JSONDecodeError, TypeError):
                    topics = [t.strip() for t in topics.split(",") if t.strip()]
            fm.setdefault("topics", topics)
        fm.setdefault("diary_id", row.get("id", 0))
        # 原子统计
        atom_count = row.get("atom_count", 0)
        if atom_count:
            fm["atom_count"] = atom_count

    # 保底字段
    if "date" not in fm:
        fm["date"] = datetime.now().strftime("%Y-%m-%d")

    # 生成 frontmatter 文本
    lines = ["---"]
    for k, v in fm.items():
        if v is None or v == "":
            continue
        lines.append(f"{k}: {_format_yaml_value(v)}")
    lines.append("---")

    return "\n".join(lines) + "\n\n" + body.strip()


def _format_yaml_value(v: Any) -> str:
    """将 Python 值格式化为 YAML 内联值"""
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        # 包含特殊字符时加引号
        if any(c in v for c in ":,#[]{}"):
            return json.dumps(v, ensure_ascii=False)
        return v
    if isinstance(v, (list, tuple)):
        # 一律输出 JSON 数组（便于解析器统一处理）
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def _sentiment_to_mood(sentiment: str) -> str:
    """将 sentiment 字段映射为 mood"""
    mapping = {
        "positive": "happy",
        "negative": "sad",
        "neutral": "neutral",
        "mixed": "mixed",
    }
    return mapping.get(sentiment.strip().lower(), sentiment.strip() or "neutral")


def mood_to_sentiment(mood: str) -> str:
    """mood → sentiment 反向映射"""
    mapping = {
        "happy": "positive",
        "sad": "negative",
        "angry": "negative",
        "neutral": "neutral",
        "mixed": "mixed",
    }
    return mapping.get(mood.strip().lower(), "neutral")


# ── [[实体]] 提取 ────────────────────────────────────────────

WIKILINK_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")


def extract_wikilinks(text: str) -> list[str]:
    """从正文中提取所有 [[实体名]]，去重、排序"""
    if not text:
        return []
    seen: set[str] = set()
    result: list[str] = []
    for m in WIKILINK_RE.finditer(text):
        name = m.group(1).strip()
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return result


def build_wikilink(entity: str) -> str:
    """为实体名生成 [[链接]] 语法"""
    return f"[[{entity}]]"


# ── 前端显示用 ────────────────────────────────────────────────

def summarize_for_list(content: str, max_len: int = 150) -> str:
    """从完整 content 中提取列表页用的摘要

    优先取 body 的前 max_len 字符，
    如果只有 frontmatter 没有 body，取前几个字段值。
    """
    _, body = parse_diary_content(content)
    if body and len(body) > 10:
        return body[:max_len]

    # 纯 frontmatter 的 fallback
    head = content.replace("---", "").strip()[:max_len]
    return head if head else "(无内容)"


def extract_mood_from_content(content: str) -> str:
    """从 content 中提取 mood 给列表页显示"""
    fm, _ = parse_diary_content(content)
    return fm.get("mood", fm.get("sentiment", ""))
