# memori — 长期记忆内核

**memori** 是一个纯净的 Python 长期记忆内核。
通过两个抽象接口（`LLMProvider` + `ContextProvider`）接入各种 Agent 框架。

## 特性

- **📝 日记式记忆** — LLM 以第一人称写日记，记录对话中的重要时刻
- **🔍 原子事实** — 结构化事实提取（episodic / factual / preference / planned / relational），FTS5 全文检索
- **🕸️ 知识图谱** — 自动构建实体关联图
- **🧠 用户画像** — 长期沉淀用户特征
- **🔀 双路检索 + RRF 融合** — BM25 文档路 + GraphEntity 图路
- **🎯 jieba 中文分词** — 精确词级匹配
- **⚡ 异步后台处理** — 不阻塞主流程
- **💡 合并 LLM 调用** — 一次调用同时输出日记 + 原子事实，减少 50% 昂贵模型调用
- **🧩 规则基原子分类** — 正则匹配原子类型，无需额外 LLM 调用，分类确定性更好
- **🔍 提前去重** — LLM 调用前检索已有记忆，内容重复则强化跳过，进一步减少不必要调用
- **🛡️ 质量校验** — 自动检测泛化词、空摘要等低质量输出，日志告警不拒写
- **🔧 JSON 自动修复** — 未闭合引号/括号/尾部逗号自动修复，容错性强

## 快速开始

### 安装

```bash
pip install memori
# 或 HTTP 服务版
pip install "memori[server]"
```

### 接入任意框架

```python
from memori import MemoryCore
from memori.core.adapters import LLMProvider, ContextProvider

class MyLLM(LLMProvider):
    async def chat(self, system_prompt: str, user_prompt: str) -> str:
        # 调用你的 LLM
        return ...

class MyCtx(ContextProvider):
    def get_user_id(self, event) -> str:
        return event.user_id
    def get_conversation_text(self, event) -> str:
        return event.text

core = MemoryCore(
    config={"bot_name": "Hana"},
    llm_provider=MyLLM(),
    context_provider=MyCtx(),
    data_dir="./data",
)
await core.initialize()
await core.process_message(user_id="user1", message_text="今天测试辛苦了")
```

### 独立 HTTP 服务

```bash
python -m memori --port 8765
```

```http
POST /api/v1/events
{
    "user_id": "123",
    "text": "今天测试辛苦了",
    "sender_name": "Hako"
}
```

API 文档：`http://localhost:8765/docs`

## 架构

```
外部事件 → 框架适配层 → MemoryCore.process_message()
                            │
                    ┌───────┴────────┐
                    │                │
              检索记忆 ↕          后台整理 ↕
                    │                │
              retriever        warm_processor
              (双路+RRF)     (Judge→去重→Capture→L3)
                    │                │
              storage ──────────────┘
         SQLite + FTS5 + Graph
```

### Capture 流程

```
消息累积 → Judge（便宜 LLM）
  │ should_remember=false → 结束
  │
  ├── 提前去重（FTS + Jaccard ≥ 0.85）
  │     └── 命中 → 强化已有记忆，跳过昂贵模型
  │
  ├── 合并 LLM 调用 → diary + atoms（单次调用）
  │     ├── JSON 自动修复
  │     ├── 质量校验（日志不拒写）
  │     └── 规则基原子分类（正则匹配类型）
  │
  └── 原子落库 + 图谱索引（异步）
```

| 模块 | 说明 |
|------|------|
| `memori.core` | 业务逻辑：MemoryCore、Retriever、WarmProcessor、Capturer… |
| `memori.storage` | SQLite 存储层：日记、原子、图谱、会话、画像 |
| `memori.models` | 数据模型：MemoryAtom、GraphNode |
| `memori.retrieval` | 双路检索：BM25 + GraphEntity + RRF |
| `memori.api` | FastAPI HTTP 服务（可选） |

## 依赖

| 包 | 用途 |
|-----|------|
| `aiosqlite` | 异步 SQLite 驱动 |
| `cachetools` | TTL 缓存 |
| `jieba` | 中文分词 |
| `fastapi` + `uvicorn` | HTTP 服务（可选） |

## License

MIT
