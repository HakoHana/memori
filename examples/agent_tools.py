"""Agent 工具集成示例

展示如何将 memori 的 `add_agent_memory` 和 `search_agent_memory`
封装为 Agent 框架的 Tool Call，让 AI Agent 在推理过程中主动读写记忆。

使用方法：
    1. 将本文件中的工具类注册到你的 Agent 框架
    2. Agent 在推理中可调用 memory_search / memorize_memory 工具
"""

from __future__ import annotations

from typing import Any


# ═══════════════════════════════════════════════════════════════
#  示例 1：LangChain / LangGraph 风格的 Tool
# ═══════════════════════════════════════════════════════════════

def create_langchain_tools(memory_core: Any) -> list:
    """为 LangChain Agent 创建记忆工具

    用法:
        from langchain.agents import initialize_agent
        tools = create_langchain_tools(memory_core)
        agent = initialize_agent(tools, llm, agent="zero-shot-react-description")
    """
    from langchain.tools import tool

    @tool
    async def memory_search(query: str, k: int = 5) -> str:
        """搜索长期记忆中的相关信息。当你需要回忆用户的偏好、经历或
        之前讨论过的事实时使用此工具。输入搜索关键词，返回相关记忆。"""
        results = await memory_core.search_agent_memory(
            user_id="current_user",
            query=query,
            k=k,
        )
        if not results:
            return "未找到相关记忆。"
        lines = []
        for r in results:
            lines.append(
                f"- [{r['type']}] {r['content']} "
                f"(重要性:{r['importance']}, 日期:{r['date']})"
            )
        return "\n".join(lines)

    @tool
    async def memorize_memory(
        memory: str,
        key_facts: list[str] | None = None,
        importance: float = 0.5,
    ) -> str:
        """将重要信息写入长期记忆。当你了解到用户的个人偏好、重要事件、
        约定或用户明确要求你记住的信息时使用此工具。

        Args:
            memory: 记忆摘要，描述发生了什么
            key_facts: 关键事实列表，每条一个独立事实
            importance: 重要性 0~1
        """
        result = await memory_core.add_agent_memory(
            user_id="current_user",
            memory=memory,
            key_facts=key_facts,
            importance=importance,
        )
        return f"已写入记忆 (ID={result['id']}, 原子={result['atom_count']}条)"

    return [memory_search, memorize_memory]


# ═══════════════════════════════════════════════════════════════
#  示例 2：OpenAI Function Calling 风格的 Tool
# ═══════════════════════════════════════════════════════════════

def get_openai_tool_definitions() -> list[dict]:
    """返回 OpenAI Function Calling 格式的工具定义

    在 Agent 的 system prompt 中加入这些工具定义，
    Agent 会在需要时调用你实现的对应函数。
    """
    return [
        {
            "type": "function",
            "function": {
                "name": "memory_search",
                "description": "搜索长期记忆，获取用户的偏好、经历和相关信息",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "搜索关键词，描述你想找的信息"
                        },
                        "k": {
                            "type": "integer",
                            "description": "返回结果数量",
                            "default": 5
                        }
                    },
                    "required": ["query"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "memorize_memory",
                "description": "将重要信息写入长期记忆，供未来参考",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "memory": {
                            "type": "string",
                            "description": "记忆摘要，描述发生了什么"
                        },
                        "key_facts": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "关键事实列表"
                        },
                        "importance": {
                            "type": "number",
                            "description": "重要性 0~1",
                            "default": 0.5
                        }
                    },
                    "required": ["memory"]
                }
            }
        }
    ]


async def handle_openai_tool_call(
    memory_core: Any,
    user_id: str,
    tool_name: str,
    arguments: dict,
) -> str:
    """处理 OpenAI 返回的工具调用

    用法:
        response = openai_client.chat(..., tools=get_openai_tool_definitions())
        if response.choices[0].finish_reason == "tool_calls":
            for tc in response.choices[0].message.tool_calls:
                result = await handle_openai_tool_call(
                    memory_core, user_id, tc.function.name, json.loads(tc.function.arguments)
                )
    """
    if tool_name == "memory_search":
        results = await memory_core.search_agent_memory(
            user_id=user_id,
            query=arguments["query"],
            k=arguments.get("k", 5),
        )
        if not results:
            return '{"found": false, "memories": []}'
        return json.dumps({"found": True, "memories": results}, ensure_ascii=False)

    elif tool_name == "memorize_memory":
        result = await memory_core.add_agent_memory(
            user_id=user_id,
            memory=arguments["memory"],
            key_facts=arguments.get("key_facts"),
            topics=arguments.get("topics"),
            sentiment=arguments.get("sentiment", "neutral"),
            importance=arguments.get("importance", 0.5),
        )
        return json.dumps(result, ensure_ascii=False)

    return '{"error": "unknown_tool"}'


# ═══════════════════════════════════════════════════════════════
#  示例 3：直接使用（无框架，最小化示例）
# ═══════════════════════════════════════════════════════════════

async def minimal_example(memory_core: Any):
    """最小化使用示例 — 不依赖任何 Agent 框架

    Agent 在对话中自行决定何时调用这些方法即可。
    """
    # Agent 决定"这值得记住"
    result = await memory_core.add_agent_memory(
        user_id="user123",
        memory="用户 Hako 告诉我他最喜欢草莓味冰淇淋，每周三晚上有钢琴课。",
        key_facts=[
            "Hako喜欢草莓味冰淇淋",
            "Hako每周三晚上上钢琴课",
        ],
        topics=["Hako", "偏好", "兴趣"],
        importance=0.7,
    )
    print(f"写入记忆: ID={result['id']}, 原子={result['atom_count']}条")

    # Agent 决定"我需要回想一下"
    memories = await memory_core.search_agent_memory(
        user_id="user123",
        query="Hako 冰淇淋 喜好",
        k=3,
    )
    for m in memories:
        print(f"  [{m['type']}] {m['content']} (重要性:{m['importance']})")


# ═══════════════════════════════════════════════════════════════
#  运行
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import asyncio
    import json

    # 初始化 memori
    from memori import MemoryCore
    from memori.core.adapters import LLMProvider

    class EchoLLM(LLMProvider):
        async def chat(self, system, user): return ""

    async def main():
        core = MemoryCore(
            config={"bot_name": "Hana"},
            llm_provider=EchoLLM(),
            context_provider=None,  # 测试用，实际需实现
            data_dir="/tmp/memori_demo",
        )
        await core.initialize()

        print("=== Agent 工具示例 ===\n")

        # 写入
        result = await core.add_agent_memory(
            user_id="demo_user",
            memory="用户说喜欢编程和钢琴，正在做一个AI项目。",
            key_facts=[
                "Hako喜欢编程",
                "Hako喜欢钢琴",
                "Hako正在开发AI项目",
            ],
            topics=["Hako", "编程", "钢琴"],
            importance=0.8,
        )
        print(f"写入: ID={result['id']}, {result['atom_count']}条原子")

        # 搜索
        results = await core.search_agent_memory(
            user_id="demo_user",
            query="编程 项目",
            k=3,
        )
        print(f"搜索到 {len(results)} 条:")
        for r in results:
            print(f"  [{r['type']}] {r['content']}")

        print("\n=== 完成 ===")

    asyncio.run(main())
