# memori — 长期记忆内核

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-208%20passing-brightgreen.svg)](tests/)

**memori** 是一个纯净的 Python 长期记忆内核，通过两个抽象接口（`LLMProvider` + `ContextProvider`）接入各种 Agent 框架。

## 特性

- **📝 日记式记忆** — LLM 以第一人称写日记，记录对话中的重要时刻
- **🔍 原子事实** — 结构化事实提取（episodic / factual / preference / planned / relational），FTS5 全文检索
- **🕸️ 知识图谱** — 自动构建实体关联图，支持共现升级为语义关系
- **🧠 用户画像** — 长期沉淀用户特征，每 N 次日记自动更新
- **🔀 双路检索 + RRF 融合** — BM25 文档路 + GraphEntity 图路
- **🎯 jieba 中文分词** — 精确词级匹配
- **⚡ 异步后台处理** — 不阻塞主流程
- **💡 合并 LLM 调用** — 一次调用同时输出日记 + 原子事实，减少 50% 昂贵模型调用
- **🧩 规则基原子分类** — 正则匹配原子类型，无需额外 LLM 调用
- **🔍 三级去重** — LLM 调用前/后/检索时三重 Jaccard 去重
- **🛡️ 质量校验** — 自动检测泛化词、空摘要，日志告警不拒写
- **🔧 JSON 自动修复** — 未闭合引号/括号/尾部逗号自动修复
- **🏗️ 接口驱动设计** — 遵循 DIP / SRP / OCP / ISP / LoD 五大原则

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

## 目录结构

```
memori/
├── memori/
│   ├── core/                 # 门面 + 接口定义（DIP）
│   │   ├── adapters.py            # LLMProvider / ContextProvider 抽象
│   │   ├── interfaces.py          # 9 个 ABC 接口
│   │   ├── memory_core.py         # 统一门面
│   │   ├── retriever.py           # 检索引擎
│   │   ├── memory_injector.py     # 记忆注入器
│   │   ├── hot_cache.py           # 热消息缓存
│   │   ├── archiver.py            # 日记归档
│   │   └── logger.py              # 日志工具
│   │
│   ├── pipeline/             # 处理流水线（SRP 拆分）
│   │   ├── capturer.py            # 抓取器：Judge→去重→Capture
│   │   ├── capture_step.py        # 策略链基类 + 4 个内置步骤（OCP）
│   │   ├── memory_uow.py          # 存储门面（LoD）
│   │   ├── atom_classifier.py     # 规则基原子分类
│   │   ├── quality_validator.py   # 输出质量校验
│   │   ├── consolidation_manager.py # 调度器
│   │   └── warm_processor.py      # 异步队列消费者
│   │
│   ├── features/             # 领域特性（SRP 拆分）
│   │   ├── graph_engine.py        # 知识图谱引擎
│   │   ├── persona_engine.py      # 用户画像引擎
│   │   └── command_handler.py     # 指令处理
│   │
│   ├── utils/                # 工具函数
│   │   ├── diary_helper.py        # 日记格式化
│   │   ├── context_formatter.py   # 时间标签
│   │   └── page_api.py            # WebUI API
│   │
│   ├── models/               # 数据模型
│   │   ├── memory_atom.py         # MemoryAtom / AtomType / 衰减计算
│   │   └── graph_models.py        # GraphNode / GraphEdge
│   │
│   ├── retrieval/            # 检索系统
│   │   ├── dual_route_retriever.py
│   │   ├── bm25_retriever.py
│   │   ├── graph_entity_retriever.py
│   │   └── rrf_fusion.py
│   │
│   ├── storage/              # 存储层
│   │   ├── atom_store.py          # FTS5 + 生命周期
│   │   ├── diary_store.py
│   │   ├── graph_store.py
│   │   ├── persona_store.py
│   │   ├── conversation_store.py
│   │   ├── state_store.py
│   │   ├── base_store.py          # 连接池 + 锁
│   │   ├── db_migration.py
│   │   ├── write_op_log.py
│   │   └── index_validator.py
│   │
│   ├── api/                  # HTTP 服务（可选）
│   └── prompts/              # LLM 提示词模板
│
├── tests/                    # 208 个测试，覆盖全部模块
│   ├── test_models.py
│   ├── test_capture_step.py
│   ├── test_capturer.py
│   ├── test_memory_uow.py
│   ├── test_atom_classifier.py
│   ├── test_quality_validator.py
│   ├── test_retriever.py
│   ├── test_interfaces.py
│   ├── test_data_flow.py
│   └── conftest.py
│
├── docs/
│   ├── ARCHITECTURE.md
│   └── configuration.md
│
├── INTERFACES.md             # 完整接口文档
├── webui/                    # Web 仪表盘
├── main.py                   # 服务入口
└── pyproject.toml
```

## 架构设计原则

| 原则 | 实现方式 |
|------|----------|
| **DIP** 依赖倒置 | `core/interfaces.py` 定义 9 个 ABC，各模块依赖接口而非具体类 |
| **SRP** 单一职责 | core / pipeline / features / utils 四包分离 |
| **OCP** 开闭原则 | `CaptureStep` 策略链，新增步骤不修改已有代码 |
| **ISP** 接口隔离 | `MemoryCoreOptions` 配置对象，调用方只传关心的字段 |
| **LoD** 最少知识 | `MemoryUnitOfWork` 门面，Capturer 不直接操作 3 个 Store |

## 核心数据流

```
消息累积 → ConsolidationManager (调度)
  │ msg_count >= 阈值
  ▼
WarmProcessor (后台异步)
  │
  ├── 1. Judge（便宜 LLM）→ 值不值得记？
  │
  ├── 2. ★ 提前去重（FTS + Jaccard ≥ 0.85）
  │       └── 命中 → 强化旧记忆，跳过昂贵模型
  │
  ├── 3. Capture（昂贵模型，合并调用）
  │     ├── 策略链：QualityCheck → AtomClassify → DiaryFill → Truncate
  │     └── 输出 diary + atoms
  │
  ├── 4. 原子落库 + 去重强化
  │
  ├── 5. 图谱索引（异步）
  │
  └── 6. 画像更新（每 N 次日记）
```

## 检索路径（同步）

```
用户消息 → Retriever.recall()
  ├── BM25 文档路（FTS5 + LIKE）
  ├── Graph 图路（实体 → 边 → 日记 → 事实）
  └── RRF 融合 → 排序结果

→ MemoryInjector.inject() → 注入到 system_prompt
→ LLM 回复（携带记忆上下文）
```

## 依赖

| 包 | 用途 |
|-----|------|
| `aiosqlite` | 异步 SQLite 驱动 |
| `cachetools` | TTL 缓存 |
| `jieba` | 中文分词 |
| `httpx` | HTTP 客户端 |
| `fastapi` + `uvicorn` | HTTP 服务（可选） |

## 更多文档

- [完整接口文档](INTERFACES.md) — 所有 ABC、数据模型、接入模板
- [架构设计](docs/ARCHITECTURE.md) — 分层、流程、设计决策
- [配置说明](docs/configuration.md) — 全部配置项

## License

MIT
