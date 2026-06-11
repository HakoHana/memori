# memori 接口文档

所有模块通过抽象接口解耦（DIP — 依赖倒置原则），接入方只需实现对应接口即可扩展或替换。

---

## 一、适配层接口（接入方必须实现）

### `LLMProvider` — 大模型调用抽象

```python
from memori.core.adapters import LLMProvider

class MyLLM(LLMProvider):
    async def chat(self, system_prompt: str, user_prompt: str) -> str:
        """调用 LLM，返回回复文本"""
        ...
```

| 方法 | 必需 | 说明 |
|---|---|---|
| `chat()` | ✅ | 主模型调用 |
| `chat_with_judge()` | ❌ | 判读模型调用，默认同 `chat` |
| `set_provider()` | ❌ | 切换主模型 |
| `set_judge_provider()` | ❌ | 切换判读模型 |

### `ContextProvider` — 事件上下文提取

```python
from memori.core.adapters import ContextProvider

class MyCtx(ContextProvider):
    def get_user_id(self, event) -> str:
        """从事件中提取用户唯一标识"""
        ...

    def get_conversation_text(self, event) -> str:
        """从事件中提取消息文本"""
        ...
```

| 方法 | 必需 | 说明 |
|---|---|---|
| `get_user_id()` | ✅ | 用户标识 |
| `get_conversation_text()` | ✅ | 对话文本 |
| `get_sender_name()` | ❌ | 发送者昵称，默认 `""` |

---

## 二、核心接口（memori.core.interfaces）

### `ICapturer` — 记忆抓取器

```python
from memori.core.interfaces import ICapturer

class MyCapturer(ICapturer):
    ...
```

| 方法 | 说明 |
|---|---|
| `capture(user_id, conversation_summary, judge_result) → CaptureResult` | 完整抓取流水线 |
| `should_capture(conversation_summary) → CaptureJudgeResult` | 判断是否值得记录 |
| `extract_atoms_for_persona(diary_content, user_id) → list[MemoryAtom]` | 为画像提取原子 |
| `apply_reinforcement(content, user_id, ...) → (bool, MemoryAtom\|None)` | 去重强化 |

### `IRetriever` — 记忆检索

```python
from memori.core.interfaces import IRetriever

class MyRetriever(IRetriever):
    ...
```

| 方法 | 说明 |
|---|---|
| `recall(user_id, query, k) → list[MemoryAtom]` | 双路检索 + RRF 融合 |
| `get_context_memories(user_id, query, k) → RecallResult` | 生成供注入用的记忆文本 |
| `get_recent_context(user_id, session_id, limit, bot_name) → str` | 最近对话上下文 |
| `search_diaries(user_id, query, k) → list[dict]` | 搜索日记全文 |
| `hybrid_search(user_id, query, k) → dict` | 混合搜索：原子 + 日记 |

### `IPersonaEngine` — 用户画像

```python
from memori.core.interfaces import IPersonaEngine
```

| 方法 | 说明 |
|---|---|
| `get_persona(uid) → str\|None` | 获取用户画像摘要（带缓存） |
| `incremental_update(uid, new_diaries, new_facts) → bool` | 增量更新 |
| `full_rebuild(uid, days) → str\|None` | 全量重建 |
| `invalidate_cache(uid)` | 清除缓存 |

### `IGraphEngine` — 知识图谱

```python
from memori.core.interfaces import IGraphEngine
```

| 方法 | 说明 |
|---|---|
| `index_diary(diary_id, content, entities)` | 从日记建立图谱索引 |
| `index_atom(atom)` | 为单条原子建立图谱索引 |
| `upgrade_cooccur_to_relates(min_count) → int` | 高频共现 → 语义关联边 |
| `batch_cooccur() → int` | 批量重建共现边 |

### `IConsolidationManager` — 调度器

```python
from memori.core.interfaces import IConsolidationManager
```

| 方法 | 说明 |
|---|---|
| `initialize()` | 从数据库恢复会话状态 |
| `on_message(user_id, conversation_text, sender_name)` | 消息入口：计数→判断→入队 |
| `destroy()` | 销毁调度器 |
| `update_config(config)` | 热更新配置 |
| `set_warm_processor(warm_processor)` | 注入 WarmProcessor |
| `get_state(user_id)` | 获取用户会话状态 |

### `IWarmProcessor` — 后台队列

```python
from memori.core.interfaces import IWarmProcessor
```

| 方法 | 说明 |
|---|---|
| `enqueue(user_id, conversation_text, state, sender_name, on_done)` | 加入整理队列 |
| `start()` | 启动消费者 |
| `stop()` | 停止消费者 |

### `ICommandHandler` — 指令处理

| 方法 | 说明 |
|---|---|
| `handle_diary(user_id, args) → str` | 查看日记 |
| `handle_diary_list(user_id, args) → str` | 日记列表 |
| `handle_memory(user_id) → str` | 查看记忆 |
| `handle_search(user_id, query) → str` | 搜索 |
| `handle_delete(user_id, args) → str` | 删除 |
| `handle_stats(user_id) → str` | 统计 |
| `handle_rebuild(user_id, args) → str` | 重建 |

### `IMemoryInjector` — 记忆注入

| 方法 | 说明 |
|---|---|
| `inject(memory_text, persona_text, system_prompt, user_message, user_name) → (str, str)` | 注入到提示词 |
| `reload_config(config)` | 热加载配置 |

### `IHotMessageCache` — 热消息缓存

| 方法 | 说明 |
|---|---|
| `push(user_id, role, content, sender_name, sender_id)` | 追加到缓存 |
| `format_recent_context(user_id, limit, bot_name) → str` | 格式化为对话文本 |
| `clear(user_id)` | 清空缓存 |

---

## 三、数据模型（memori.models.memory_atom）

### `MemoryAtom` — 记忆原子

```python
@dataclass
class MemoryAtom:
    user_id: str
    diary_date: str               # YYYY-MM-DD
    content: str
    atom_type: AtomType           # episodic / factual / preference / planned / relational
    entities: list[str]
    importance: float             # 0.0 ~ 1.0
    confidence: float             # 0.0 ~ 1.0
    expires_at: float             # 过期时间戳
    status: AtomStatus            # active / dormant / archived / forgotten
    diary_snippet: str            # 溯源片段
    diary_id: int                 # 关联日记 ID
    atom_id: int                  # 数据库 ID
```

### `AtomType` — 原子类型

```python
class AtomType(str, Enum):
    EPISODIC = "episodic"       # 情景/事件
    FACTUAL = "factual"         # 客观事实
    PREFERENCE = "preference"   # 偏好/喜好
    PLANNED = "planned"         # 计划/约定
    RELATIONAL = "relational"   # 社交关系
```

### `CaptureJudgeResult` — 判读结果

```python
@dataclass
class CaptureJudgeResult:
    should_remember: bool
    reason: str
    importance: float            # 0.0 ~ 1.0
    mood: str                    # happy / sad / angry / excited / neutral / mixed
    context_summary: str
```

### `CaptureResult` — 抓取结果

```python
@dataclass
class CaptureResult:
    wrote_diary: bool
    diary_content: str
    atoms: list[MemoryAtom]
    atom_count: int              # property
```

### `RecallResult` — 检索结果

```python
@dataclass
class RecallResult:
    memory_text: str             # 供 LLM 注入的格式化文本
    atoms: list[MemoryAtom]     # 召回原子
    persona_text: str | None    # 用户画像
```

---

## 四、策略链（memori.pipeline.capture_step）

Capture 流水线采用策略模式，每步实现 `CaptureStep` 抽象基类：

```python
class CaptureStep(ABC):
    @abstractmethod
    async def process(self, ctx: CaptureContext) -> CaptureContext:
        ...
```

| 内置步骤 | 用途 | 启用条件 |
|---|---|---|
| `QualityCheckStep` | 检测泛化词、空内容 | `config.enable_quality_check` |
| `AtomClassifyStep` | 规则基原子分类或 LLM 回退 | `config.enable_rule_classifier` |
| `DiaryFillStep` | 空日记时从实体填充占位 | 总是启用 |
| `TruncateStep` | 按重要度取 top N | 总是启用 |

新增步骤示例：

```python
from memori.pipeline.capture_step import CaptureStep, CaptureContext

class SentimentStep(CaptureStep):
    async def process(self, ctx: CaptureContext) -> CaptureContext:
        ctx.quality_warnings.append("sentiment_checked")
        return ctx

# 注册到 Capturer
capturer._capture_steps.insert(2, SentimentStep())
```

---

## 五、存储门面（memori.pipeline.memory_uow）

### `MemoryUnitOfWork` — Capturer 专用门面

遵循最少知识原则，Capturer 不直接操作存储层。

| 方法 | 说明 |
|---|---|
| `append_diary(user_id, date, content) → int` | 写入日记 |
| `update_diary_importance(user_id, date, importance)` | 更新日记重要度 |
| `count_active_atoms(diary_id) → int` | 活跃原子数 |
| `search_fts(query, user_id, k) → list[MemoryAtom]` | FTS 搜索 |
| `insert_atoms(atoms) → list[int]` | 批量插入原子 |
| `reinforce_atom(atom_id, importance, confidence, expires_at)` | 强化原子 |
| `delete_forgotten_atom(atom_id)` | 删除已遗忘原子 |
| `ensure_fact(content, atom_type, importance, confidence) → int` | 确保事实存在 |
| `link_fact(diary_id, fact_id, importance, snippet)` | 关联事实到日记 |

---

## 六、配置对象（memori.core.memory_core）

### `MemoryCoreOptions` — 启动配置

```python
@dataclass
class MemoryCoreOptions:
    config: dict | None                 # 全局配置字典
    data_dir: str | None                # 数据目录
    reply_handler: Callable | None      # 回复回调
    # 存储层覆盖（留空时从 data_dir 自动创建）
    atom_store: AtomStore | None
    diary_store: DiaryStore | None
    persona_store: PersonaStore | None
    state_store: StateStore | None
    graph_store: GraphStore | None
    conversation_store: ConversationStore | None
    write_op_log: WriteOpLog | None
```

### `MemoryCore` — 统一门面

```python
# 标准启动方式（推荐）
core = MemoryCore(
    llm_provider=MyLLM(),
    context_provider=MyCtx(),
    options=MemoryCoreOptions(
        config={"bot_name": "Hana"},
        data_dir="./data",
    ),
)
await core.initialize()

# 完整参数
await core.process_message(
    user_id="user1",
    message_text="今天测试辛苦了",
    sender_name="Hako",
    session_id="sess_001",
)
```

---

## 七、全局配置选项

| Key | 默认值 | 说明 |
|---|---|---|
| `bot_name` | `"Hana"` | AI 名称 |
| `max_diary_tokens` | `500` | 日记最大 token |
| `max_atoms_per_capture` | `5` | 每次最多提取原子数 |
| `enable_quality_check` | `True` | 启用质量校验 |
| `enable_rule_classifier` | `True` | 启用规则基分类 |
| `recall_count` | `5` | 检索返回数量 |
| `recall_max_tokens` | `500` | 检索文本最大 token |

---

## 八、快速接入模板

```python
from memori import MemoryCore
from memori.core.adapters import LLMProvider, ContextProvider
from memori.core.memory_core import MemoryCoreOptions

class MyLLM(LLMProvider):
    async def chat(self, system_prompt: str, user_prompt: str) -> str:
        return "LLM response here"

class MyCtx(ContextProvider):
    def get_user_id(self, event) -> str: return event.user_id
    def get_conversation_text(self, event) -> str: return event.text

# 初始化
core = MemoryCore(
    llm_provider=MyLLM(),
    context_provider=MyCtx(),
    options=MemoryCoreOptions(config={"bot_name": "助手"}, data_dir="./data"),
)
await core.initialize()

# 处理消息
await core.process_message(user_id="user1", message_text="你好", sender_name="用户")

# 检索记忆
from memori.core.interfaces import IRetriever
memories = await core.retriever.get_context_memories("user1", "你好")
print(memories.memory_text)
```
