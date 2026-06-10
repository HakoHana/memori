"""AstrBot Agent 工具 — 让 LLM 主动搜索/写入记忆"""

from __future__ import annotations

import json
import time

from astrbot.core.agent.tool import FunctionTool

from memori import MemoryCore
from memori.models.memory_atom import MemoryAtom, AtomType


def _json_result(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


class RecallTool(FunctionTool):
    """搜索长期记忆"""

    def __init__(self):
        super().__init__(
            name="recall_long_term_memory",
            description="当对话需要参考长期记忆中的信息时，调用此工具搜索相关记忆。"
            "使用简短的关键词，不要复制整个用户消息。"
            "当用户问「你还记得吗」「之前说的」「帮我回忆」等时，优先调用此工具。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "k": {"type": "integer", "description": "返回结果数量", "default": 3},
                },
                "required": ["query"],
            },
        )
        self._core = None

    def set_core(self, core: MemoryCore):
        self._core = core

    async def call(self, context, **kwargs) -> str:
        core = self._core
        if not core:
            return _json_result({"count": 0, "error": "core not set"})

        query = kwargs.get("query", "").strip()
        k = int(kwargs.get("k", 3))
        if not query:
            return _json_result({"count": 0, "error": "query is empty"})

        try:
            uid = "default"
            atoms = await core.retriever.recall(uid, query, k)
            results = [
                {"content": a.content, "type": a.atom_type.value,
                 "importance": a.importance, "date": a.diary_date}
                for a in atoms
            ]
            return _json_result({"count": len(results), "results": results})
        except Exception as e:
            return _json_result({"count": 0, "error": str(e)})


class MemorizeTool(FunctionTool):
    """主动写入记忆"""

    def __init__(self):
        super().__init__(
            name="memorize_long_term_memory",
            description="当用户明确要求你记住某些信息时（如「帮我记住」「别忘了」「请记住」），"
            "调用此工具将信息写入长期记忆。"
            "将信息整理为简洁的一句话或几个关键点。",
            parameters={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "要记住的信息内容"},
                    "importance": {"type": "number", "description": "重要度（0~1）", "default": 0.7},
                },
                "required": ["content"],
            },
        )
        self._core = None

    def set_core(self, core: MemoryCore):
        self._core = core

    async def call(self, context, **kwargs) -> str:
        core = self._core
        if not core:
            return _json_result({"success": False, "error": "core not set"})

        content = kwargs.get("content", "").strip()
        importance = float(kwargs.get("importance", 0.7))
        if not content:
            return _json_result({"success": False, "error": "content is empty"})

        try:
            uid = "default"
            today = time.strftime("%Y-%m-%d")
            diary = await core.diary_store.read(uid, today)
            if not diary:
                await core.diary_store.append(uid, today, f"## {time.strftime('%H:%M')}\n\n{content}")

            atom = MemoryAtom(
                user_id=uid,
                diary_date=today,
                content=content[:200],
                atom_type=AtomType.FACTUAL,
                importance=importance,
            )
            atom.prepare_insert()
            aid = await core.atom_store.insert(atom)
            if core.graph_engine:
                await core.graph_engine.index_atom(atom)
            return _json_result({"success": True, "id": aid, "content": content})
        except Exception as e:
            return _json_result({"success": False, "error": str(e)})
