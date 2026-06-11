# Memori 架构文档

**版本**: 0.2.0
**更新日期**: 2026-06-11

---

## 概述

Memori 是一个纯净的 Python 长期记忆内核，从对话中提取、存储、检索记忆。核心设计遵循五项面向对象原则：

| 原则 | 含义 | 实现 |
|------|------|------|
| **DIP** | 依赖倒置 | 9 个 ABC 接口定义在 `core/interfaces.py`，模块依赖接口而非实现 |
| **SRP** | 单一职责 | core / pipeline / features / utils 四包分离 |
| **OCP** | 开闭原则 | `CaptureStep` 策略链，新建步骤只需新增子类 |
| **ISP** | 接口隔离 | `MemoryCoreOptions` 配置对象，调用方只关注关心的字段 |
| **LoD** | 最少知识 | `MemoryUnitOfWork` 门面，Capturer 只与一个门面交互 |

### 设计目标

- **零框架依赖** — 核心纯 Python，`adapters.py` 定义接口由外部实现
- **异步非阻塞** — 记忆整理在后台队列执行，不阻塞实时回复
- **三级存储** — 日记（叙事）+ 原子（结构化）+ 图谱（实体关联）
- **双路检索** — BM25 文档路 + GraphEntity 图路 + RRF 融合

---

## 分层架构

```
┌──────────────────────────────────────────────────────┐
│                   外部框架层                           │
│   AstrBot / NoneBot / 自有框架                         │
│   实现 LLMProvider + ContextProvider 接口               │
└──────────────────┬───────────────────────────────────┘
                   │ 事件
┌──────────────────▼───────────────────────────────────┐
│               MemoryCore（门面）                        │
│   process_message() / trigger_capture()                 │
│   ┌────────────────────────────────────────────────┐   │
│   │  core/                                        │   │
│   │  ├── interfaces.py  ← 9 个 ABC 接口            │   │
│   │  ├── retriever.py                             │   │
│   │  ├── memory_injector.py                       │   │
│   │  ├── hot_cache.py                             │   │
│   │  └── archiver.py                              │   │
│   └────────────────────────────────────────────────┘   │
├──────────┬─────────────────────┬───────────────────────┤
│  检索路径  │      捕获路径        │     存储路径           │
│  (同步)   │    (后台异步)        │                       │
├──────────┼─────────────────────┼───────────────────────┤
│Retriever │ ConsolidationMgr   │ Schema: memory_atoms   │
│Injector  │  → WarmProcessor   │         diary_entries  │
│          │    → Judge         │         graph_nodes    │
│          │    → 提前去重       │         graph_edges    │
│          │    → Capture       │         personas       │
│          │      └ 策略链       │         conversations  │
│          │    → PersonaUpdate  │         sessions       │
│          │    → GraphIndex    │                       │
└──────────┴─────────────────────┴───────────────────────┘
```

---

## 目录结构

```
memori/
├── core/                  # 门面 + 接口
│   ├── adapters.py             # LLMProvider / ContextProvider 抽象
│   ├── interfaces.py           # 9 个 ABC 接口（DIP 基座）
│   ├── memory_core.py          # 统一门面 + MemoryCoreOptions（ISP）
│   ├── retriever.py            # 检索引擎（IRetriever）
│   ├── memory_injector.py      # 记忆注入器（IMemoryInjector）
│   ├── hot_cache.py            # 热消息缓存（IHotMessageCache）
│   └── archiver.py             # 日记归档
│
├── pipeline/              # 处理流水线（SRP 拆分）
│   ├── capturer.py             # 抓取器（ICapturer）
│   ├── capture_step.py         # 策略链基类（OCP）+ 4 内置步骤
│   ├── memory_uow.py           # 存储门面（LoD）
│   ├── atom_classifier.py      # 规则基原子分类
│   ├── quality_validator.py    # 输出质量校验
│   ├── consolidation_manager.py # 调度器（IConsolidationManager）
│   └── warm_processor.py       # 后台队列（IWarmProcessor）
│
├── features/              # 领域特性（SRP 拆分）
│   ├── graph_engine.py         # 图谱引擎（IGraphEngine）
│   ├── persona_engine.py       # 画像引擎（IPersonaEngine）
│   └── command_handler.py      # 指令处理（ICommandHandler）
│
├── utils/                 # 工具函数
│   ├── diary_helper.py         # 日记 frontmatter 格式化
│   ├── context_formatter.py    # 时间标签
│   └── page_api.py             # WebUI API
│
├── models/                # 数据模型
│   ├── memory_atom.py          # MemoryAtom / AtomType / 衰减
│   └── graph_models.py         # GraphNode / GraphEdge
│
├── retrieval/             # 检索系统
│   ├── dual_route_retriever.py  # 双路编排
│   ├── bm25_retriever.py        # FTS5 + LIKE
│   ├── graph_entity_retriever.py # 图路遍历
│   └── rrf_fusion.py            # Reciprocal Rank Fusion
│
├── storage/               # 存储层（SQLite + FTS5 + WAL）
│   ├── base_store.py            # 连接池 + asyncio.Lock
│   ├── atom_store.py            # 原子 CRUD + FTS5
│   ├── diary_store.py           # 日记 CRUD + FTS
│   ├── graph_store.py           # 节点/边 CRUD
│   ├── persona_store.py
│   ├── conversation_store.py
│   ├── state_store.py
│   ├── write_op_log.py          # 崩溃恢复日志
│   ├── db_migration.py
│   └── index_validator.py
│
├── api/                   # FastAPI HTTP（可选）
├── prompts/               # LLM 提示词
│
tests/                     # 208 个测试
docs/                      # 文档
```

---

## 依赖关系（重构后）

```
core/memory_core.py (门面)
  │
  ├── core/retriever.py  →  retrieval/  →  storage/
  │
  ├── core/hot_cache.py
  │
  ├── pipeline/consolidation_manager.py
  │     └── pipeline/warm_processor.py
  │           ├── pipeline/capturer.py
  │           │     ├── pipeline/memory_uow.py  →  storage/
  │           │     ├── pipeline/capture_step.py
  │           │     │     ├── pipeline/atom_classifier.py
  │           │     │     └── pipeline/quality_validator.py
  │           │     └── core/adapters.py  (LLMProvider)
  │           │
  │           ├── features/graph_engine.py  →  storage/
  │           └── features/persona_engine.py
  │
  ├── core/memory_injector.py
  │
  └── features/command_handler.py
```

**关键变化**（相对于 v0.1）：

| 模块 | 旧位置 | 新位置 |
|------|--------|--------|
| 原子分类 | `core/atom_classifier.py` | `pipeline/atom_classifier.py` |
| 质量校验 | `core/quality_validator.py` | `pipeline/quality_validator.py` |
| 图谱引擎 | `core/graph_engine.py` | `features/graph_engine.py` |
| 画像引擎 | `core/persona_engine.py` | `features/persona_engine.py` |
| 指令处理 | `core/command_handler.py` | `features/command_handler.py` |
| 日记工具 | `core/diary_helper.py` | `utils/diary_helper.py` |
| 上下文格式 | `core/context_formatter.py` | `utils/context_formatter.py` |
| 接口定义 | 分散在各模块 | `core/interfaces.py` 集中 |

---

## 核心流程

### 消息处理路径（同步，阻塞回复）

```
用户消息
  │
  ├── HotCache.push()                    写入热缓存
  │
  ├── Retriever.get_context_memories()
  │     ├── recall()                     双路检索 + RRF 融合
  │     │   ├── BM25Retriever            FTS5（英文）+ LIKE（中文）
  │     │   ├── GraphEntityRetriever     实体→边→日记→事实
  │     │   └── RRF 融合                 Reciprocal Rank Fusion
  │     ├── persona_store.read()         读取画像
  │     └── 组装记忆文本
  │
  ├── MemoryInjector.inject()            注入到 system_prompt
  │
  └── 返回修改后的消息 → LLM 响应
```

### 记忆捕获路径（后台异步，不阻塞回复）

```
消息累积 ConsolidationManager
  │ msg_count >= 阈值 或 超时
  ▼
WarmProcessor._process_one()
  │
  ├── 1. Judge（便宜 LLM）
  │     └── should_remember? → 否则结束
  │
  ├── 2. ★ 提前去重
  │     apply_reinforcement(threshold=0.85)
  │     ├── FTS 检索 + Jaccard 比对
  │     └── 命中 → 强化旧记忆，跳过 Capture
  │
  ├── 3. Capture（昂贵模型）
  │     _merged_capture()
  │     └── CaptureStep 策略链:
  │          ① QualityCheckStep  — 质量校验
  │          ② AtomClassifyStep  — 规则分类或 LLM 回退
  │          ③ DiaryFillStep     — 空日记占位
  │          ④ TruncateStep      — 取 top N
  │
  ├── 4. 原子落库 + 去重
  │     └── MemoryUnitOfWork 门面
  │           ├── insert_atoms()
  │           ├── reinforce_atom()
  │           ├── ensure_fact() + link_fact()
  │           └── delete_forgotten_atom()
  │
  ├── 5. 图谱索引（异步 fire-and-forget）
  │     └── graph_engine.index_diary()
  │
  └── 6. 画像更新（每 10 次日记触发）
        └── persona_engine.incremental_update()
```

---

## 接口体系（DIP）

9 个 ABC 接口集中在 `core/interfaces.py`，实现在各自模块中。

```
                  ┌───────────────────┐
                  │   ICapturer       │  ← pipeline/capturer.py
                  ├───────────────────┤
                  │   IRetriever      │  ← core/retriever.py
                  ├───────────────────┤
                  │   IPersonaEngine  │  ← features/persona_engine.py
                  ├───────────────────┤
                  │   IGraphEngine    │  ← features/graph_engine.py
                  ├───────────────────┤
                  │   ICommandHandler │  ← features/command_handler.py
                  ├───────────────────┤
                  │   IMemoryInjector │  ← core/memory_injector.py
                  ├───────────────────┤
                  │   IWarmProcessor  │  ← pipeline/warm_processor.py
                  ├───────────────────┤
                  │ IConsolidationMgr │  ← pipeline/consolidation_manager.py
                  ├───────────────────┤
                  │ IHotMessageCache  │  ← core/hot_cache.py
                  └───────────────────┘
```

所有实现类均通过接口契约测试验证（`tests/test_interfaces.py`），确保：
- 每个实现类是其接口的子类
- 所有抽象方法都已实现
- 方法签名匹配

---

## 策略链（OCP）

`CaptureStep` 抽象基类让 Capture 流程可扩展而无需修改 `capturer.py`：

```
CaptureContext 流经策略链:
  diary_body + raw_atom_dicts + atoms

    ① QualityCheckStep         仅记录警告，不拒写
              │
    ② AtomClassifyStep         规则基（正则）或 LLM 回退
              │
    ③ DiaryFillStep            diary 为空时从实体生成占位
              │
    ④ TruncateStep             按重要度降序取 top N
              │
    输出: diary_body + atoms
```

**新增步骤**：只需新建 `CaptureStep` 子类 → 注册到 `Capturer._capture_steps`。

---

## 存储门面（LoD）

`MemoryUnitOfWork` 封装 `DiaryStore` + `AtomStore` + `WriteOpLog`，Capturer 只与一个门面交互：

```python
# 之前：Capturer 直接操作 3 个 Store
self.diary_store.append(...)
self.atom_store.search_fts(...)
self.write_op_log.begin(...)

# 之后：通过门面 1 个对象
self._store.append_diary(...)
self._store.search_fts(...)
self._store.begin_op(...)
```

---

## 配置对象（ISP）

`MemoryCoreOptions` 将可选参数聚合为一个 dataclass：

```python
core = MemoryCore(
    llm_provider=...,
    context_provider=...,
    options=MemoryCoreOptions(
        config={"bot_name": "Hana"},
        data_dir="./data",
        # 其他 7 个可选字段
    ),
)
```

调用方只关注需要覆盖的字段，其余使用默认值。

---

## 数据模型

### MemoryAtom（记忆原子）

| 字段 | 类型 | 说明 |
|------|------|------|
| `content` | str | 事实陈述，第三人称客观 |
| `atom_type` | AtomType | episodic / factual / preference / planned / relational |
| `entities` | list[str] | 涉及实体名 |
| `importance` | float | 0.0 ~ 1.0 |
| `confidence` | float | 0.0 ~ 1.0 |
| `diary_snippet` | str | 溯源原文 |
| `diary_id` | int | 关联日记 ID |
| `status` | AtomStatus | active / dormant / archived / forgotten |
| `expires_at` | float | 过期时间戳 |

**TTL 策略**：

| 类型 | 基础 TTL | 衰减方式 |
|------|----------|----------|
| EPISODIC（事件） | 30 天 | 指数衰减 |
| FACTUAL（事实） | 180 天 | 指数衰减 |
| PREFERENCE（偏好） | 60 天 | 指数衰减 |
| PLANNED（计划） | 7 天 | 阶梯衰减 |
| RELATIONAL（关系） | 90 天 | 线性衰减 |

---

## 关键设计决策

### 1. 合并 LLM 调用

**问题**：旧流程中写日记和提取原子是两次独立昂贵模型调用。

**方案**：`merged.txt` prompt 让模型一次输出 JSON `{"diary": "...", "atoms": [...]}`。

**效果**：昂贵模型调用减少约 50%。三级降级：完整 JSON → 部分提取 → 分步回退。

### 2. 规则基原子分类

**问题**：LLM 输出 `type`/`importance`/`confidence` 浪费 token，分类不一致。

**方案**：LLM 只输出 `content` + `entities`，类型由 `atom_classifier.py` 的正则模式匹配确定。

| 模式 | 分类 | 置信度 |
|------|------|--------|
| 未来时间词 + 行动动词 | PLANNED | 0.85 |
| 过去时间词 + 行动动词 | EPISODIC | 0.80 |
| 偏好关键词 | PREFERENCE | 0.82 |
| 关系关键词 | RELATIONAL | 0.80 |
| 状态性描述（是/有） | FACTUAL | 0.78 |
| 仅有行动动词 | EPISODIC | 0.75 |
| 无匹配 | FACTUAL | 0.65 |

### 3. 三级去重策略

| 层级 | 位置 | 阈值 | 目的 |
|------|------|------|------|
| L0 | 检索时 `get_context_memories` | 精确 + Jaccard 0.6 | 召回结果去重 |
| L1 | Capture 前提前去重 | Jaccard ≥ 0.85 | 跳过昂贵模型 |
| L2 | Capture 后强化去重 | Jaccard ≥ 0.6 | 兜底强化 |

### 4. 强化策略

- **步长递减**：首次 +0.05，第 N 次 `0.05 / log2(n+2)` → 0.01
- **融合 Judge**：`boosted = max(step_boosted, judge*0.7 + old*0.3)`
- **TTL 延长**：`expires_at` 延长 30%
- **日记同步**：`UPDATE diary_entries SET importance = MAX(importance, ?)`

### 5. 双路检索 + RRF

```
BM25 文档路（memory_atoms FTS5）
  ├── ASCII 词 → FTS5 全文检索（OR 连接）
  └── CJK 词   → LIKE %kw%（jieba 分词后）

GraphEntity 图路（graph_nodes → graph_edges → diary）
  ├── 关键词匹配 entity 节点
  ├── 沿 mentions 边找到日记
  └── 日记 → 回溯关联原子

RRF 融合：
  score = Σ 1 / (RRF_K + rank)   对两路结果重排序
  RRF_K = 60
```

### 6. JSON 自动修复

`_fix_json()` 处理 LLM 输出的常见 JSON 损坏：

- 移除 markdown 代码块包裹
- 补全未闭合引号
- 补全未闭合方括号/花括号
- 移除尾部逗号
- 转义控制字符

### 7. 写操作日志（崩溃恢复）

```python
op_id = await store.begin_op("capture", {"user_id": uid})
await store.step_op(op_id, "diary_written")
await store.step_op(op_id, "atoms_stored")
await store.complete_op(op_id)
```

每条 Capture 记录三步日志，崩溃后可根据未完成的操作恢复。

---

## 测试覆盖

208 个测试覆盖全部模块：

| 测试文件 | 覆盖模块 | 数量 |
|----------|----------|------|
| `test_models.py` | MemoryAtom、衰减算法 | 28 |
| `test_atom_classifier.py` | 规则分类、时间解析 | 16 |
| `test_quality_validator.py` | 质量校验 | 17 |
| `test_capture_step.py` | 策略链每步独立验证 | 14 |
| `test_memory_uow.py` | 存储门面 | 12 |
| `test_capturer.py` | Capturer 集成 | 17 |
| `test_retriever.py` | 检索 + RRF | 18 |
| `test_interfaces.py` | 接口契约 | 14 |
| `test_data_flow.py` | 端到端数据流 | 7 |

运行方式：

```bash
pytest tests/ -v
```

---

## 扩展指南

### 接入新框架

```python
from memori import MemoryCore
from memori.core.adapters import LLMProvider, ContextProvider

class MyLLM(LLMProvider):
    async def chat(self, system, user) -> str: ...
    async def chat_with_judge(self, system, user) -> str: ...  # 可选

class MyCtx(ContextProvider):
    def get_user_id(self, event) -> str: ...
    def get_conversation_text(self, event) -> str: ...

core = MemoryCore(
    llm_provider=MyLLM(),
    context_provider=MyCtx(),
    options=MemoryCoreOptions(data_dir="./data"),
)
await core.initialize()
```

### 替换存储实现

```python
from memori.storage.atom_store import AtomStore

class MyAtomStore(AtomStore):
    async def search_fts(self, query, user_id, k):
        # 使用向量数据库
        ...

core = MemoryCore(
    ...,
    options=MemoryCoreOptions(atom_store=MyAtomStore(":memory:")),
)
```

### 添加策略步骤

```python
from memori.pipeline.capture_step import CaptureStep, CaptureContext

class SentimentCheckStep(CaptureStep):
    async def process(self, ctx: CaptureContext) -> CaptureContext:
        # 自定义逻辑
        return ctx

capturer._capture_steps.insert(2, SentimentCheckStep())
```

---

## 版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| 0.2.0 | 2026-06-11 | 架构重构：DIP/SRP/OCP/ISP/LoD 五项原则；策略链、存储门面、配置对象 |
| 0.1.0 | 2026-06-10 | 初始版本 |
