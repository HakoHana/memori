"""Agent 记忆工具 — 让 LLM 可以主动搜索和写入记忆"""


import json
from typing import Any

from .logger import logger
from .context import current_user_id
from astrbot.core.agent.tool import FunctionTool

# memory_core 不在 __init__ 中传入（Pydantic v2 限制），
# 在主流程中通过 set_memory_core() 注入


def _get_uid() -> str:
    """从上下文中获取当前用户 ID，兜底配置"""
    uid = current_user_id.get()
    if uid:
        return uid
    logger.warning("current_user_id 为空，使用默认")
    return "default"


def _json_result(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


class RecallMemoryTool(FunctionTool):
    """主动搜索记忆工具"""

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

    def set_memory_core(self, mc):
        """注入 MemoryCore 实例"""
        object.__setattr__(self, '_memory_core', mc)

    async def call(self, **kwargs) -> str:
        mc = self._memory_core
        if not mc:
            return _json_result({"count": 0, "results": [], "error": "memory_core not set"})

        query = kwargs.get("query", "").strip()
        k = int(kwargs.get("k", 3))
        if not query:
            return _json_result({"count": 0, "results": [], "error": "query is empty"})

        try:
            user_id = _get_uid()
            atoms = await mc.retriever.recall(user_id, query, k)
            results = [
                {"content": a.content, "type": a.atom_type.value, "importance": a.importance, "date": a.diary_date}
                for a in atoms
            ]
            return _json_result({"count": len(results), "results": results})
        except Exception as e:
            logger.error(f"RecallMemoryTool error: {e}")
            return _json_result({"count": 0, "results": [], "error": str(e)})


class MemorizeMemoryTool(FunctionTool):
    """主动写入记忆工具"""

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

    def set_memory_core(self, mc):
        """注入 MemoryCore 实例"""
        object.__setattr__(self, '_memory_core', mc)

    async def call(self, **kwargs) -> str:
        mc = self._memory_core
        if not mc:
            return _json_result({"success": False, "error": "memory_core not set"})

        content = kwargs.get("content", "").strip()
        importance = float(kwargs.get("importance", 0.7))
        if not content:
            return _json_result({"success": False, "error": "content is empty"})

        try:
            from ..models.memory_atom import MemoryAtom, AtomType
            import time

            uid = _get_uid()
            today = time.strftime("%Y-%m-%d")

            diary = await mc.diary_store.read(uid, today)
            if not diary:
                await mc.diary_store.append(
                    uid, today, f"## {time.strftime('%H:%M')}\n\n{content}"
                )

            atom = MemoryAtom(
                user_id=uid,
                diary_date=today,
                content=content[:200],
                atom_type=AtomType.FACTUAL,
                importance=importance,
            )
            atom.prepare_insert()
            aid = await mc.atom_store.insert(atom)

            if mc.graph_engine:
                await mc.graph_engine.index_atom(atom)

            return _json_result({"success": True, "id": aid, "content": content})
        except Exception as e:
            logger.error(f"MemorizeMemoryTool error: {e}")
            return _json_result({"success": False, "error": str(e)})
