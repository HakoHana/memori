# memori 画像系统 — 架构与数据流

> 本文档基于 `memori/memori/` 运行时版本（无 HotMessageCache 版）

---

## 一、系统架构图

```mermaid
graph TB
    subgraph External["外部接入层"]
        AstrBot["AstrBot 插件<br/>main.py"]
        FastAPI["FastAPI 服务<br/>memori/api/"]
        Dashboard["Dashboard<br/>Web 面板"]
    end

    subgraph Core["核心业务层"]
        MC["MemoryCore<br/>统一门面"]
        RI["Retriever<br/>检索引擎"]
        MJ["MemoryInjector<br/>记忆注入器"]
        GE["GraphEngine<br/>图谱引擎"]
        PE["PersonaEngine<br/>画像引擎"]
    end

    subgraph Pipeline["后台流水线层"]
        CM["ConsolidationManager<br/>调度器<br/>(轮数/空闲/定时触发)"]
        WP["WarmProcessor<br/>异步队列消费者<br/>(限速 60s/用户)"]
        CP["Capturer<br/>抓取器<br/>(Judge→日记→原子)"]
        MU["MemoryUnitOfWork<br/>写操作门面"]
    end

    subgraph Storage["持久化层"]
        AS["AtomStore<br/>原子存储 + FTS5<br/>+ atoms_diary_links"]
        DS["DiaryStore<br/>日记存储 + FTS"]
        PS["PersonaStore<br/>画像存储"]
        CS["ConversationStore<br/>对话历史<br/>(滑动窗口保留)"]
        GS["GraphStore<br/>图谱节点/边"]
        SS["StateStore<br/>会话状态"]
        WL["WriteOpLog<br/>写操作日志"]
    end

    subgraph DB["SQLite 数据库"]
        DB1["memory.db<br/>atoms/facts/persona/users/links"]
        DB2["diaries.db<br/>diary_entries"]
        DB3["conversations.db<br/>sessions/messages"]
        DB4["graph.db<br/>nodes/edges"]
        DB5["state.db<br/>consolidation_state"]
    end

    subgraph LLM["LLM 提供商"]
        L1["主模型<br/>写日记/提取原子"]
        L2["判读模型<br/>Judge 判断"]
        L3["嵌入模型<br/>向量检索"]
    end

    subgraph Lifecycle["生命周期管理"]
        DEC["Decay<br/>重要性衰减"]
        ARC["Archiver<br/>归档"]
        CLN["Cleanup<br/>清理孤立原子"]
        DED["Dedup<br/>去重强化"]
    end

    %% External 连接
    AstrBot --> MC
    FastAPI --> MC
    Dashboard --> FastAPI

    %% Core 内部依赖
    MC --> RI
    MC --> MJ
    MC --> GE
    MC --> PE
    MC --> CM
    MC --> WP

    %% Pipeline 流水线
    CM --> WP
    WP --> CP
    CP --> MU
    MU --> AS
    MU --> DS
    MU --> WL

    %% 检索流程
    RI --> AS
    RI --> GS
    RI --> PS
    RI --> CS

    %% 图谱
    CP -- "回调: index_diary()" --> GE
    GE --> GS

    %% Persona
    WP --> PE
    PE --> AS

    %% 注入
    RI --> MJ

    %% 存储 → 数据库
    AS --> DB1
    DS --> DB2
    CS --> DB3
    GS --> DB4
    SS --> DB5

    %% LLM
    CP --> L1
    CP --> L2
    CP --> L3
    PE --> L1

    %% 生命周期
    MC --> Lifecycle
    Lifecycle --> AS
    Lifecycle --> DS

    %% 样式
    classDef external fill:#e1f5fe,stroke:#01579b
    classDef core fill:#fff3e0,stroke:#e65100
    classDef pipeline fill:#e8f5e9,stroke:#1b5e20
    classDef storage fill:#f3e5f5,stroke:#4a148c
    classDef db fill:#fce4ec,stroke:#b71c1c
    classDef llm fill:#fff8e1,stroke:#f57f17
    classDef lifecycle fill:#fbe9e7,stroke:#bf360c
    class AstrBot,FastAPI,Dashboard external
    class MC,RI,MJ,GE,PE core
    class CM,WP,CP,MU pipeline
    class AS,DS,PS,CS,GS,SS,WL storage
    class DB1,DB2,DB3,DB4,DB5 db
    class L1,L2,L3 llm
    class DEC,ARC,CLN,DED lifecycle
```

---

## 二、消息处理数据流（实时路径）

```mermaid
sequenceDiagram
    participant User as 用户
    participant Plugin as AstrBot Plugin
    participant CS as ConversationStore
    participant MC as MemoryCore
    participant RT as Retriever
    participant AS as AtomStore
    participant GS as GraphStore
    participant DS as DiaryStore
    participant MJ as MemoryInjector
    participant LLM as LLM

    User->>Plugin: 发送消息

    Note over Plugin: on_llm_request filter
    Plugin->>Plugin: _ensure_user_identity()

    par 后台: 写入对话历史
        Plugin->>CS: add_message(session_id, uid, role=user, content)
    end

    Plugin->>MC: process_message(uid, text, name)

    alt 以 / 开头 → 指令处理
        MC->>MC: _handle_command(/日记 /记忆 ...)
        MC-->>Plugin: 返回 None
    else 普通消息
        MC->>RT: get_context_memories(uid, text)

        rect rgb(232, 245, 233)
            Note over RT: 1. 多路检索（全局搜索，不限 user_id）
            RT->>GS: graph_keyword_retrieve(keywords)
            RT->>AS: bm25_retrieve(keywords)
            alt 启用了嵌入模型
                RT->>AS: vector_retrieve(query_embed)
                RT->>GS: graph_vector_retrieve(keywords)
            end
            Note over RT: RRF 融合 → 候选池 (default 50/路)
        end

        rect rgb(255, 243, 224)
            Note over RT: 2. 社交加权重排序
            RT->>GS: 查 friend_of 边 → friend_weights
            Note over RT: final_score = relevance × (1 + α × weight)<br/>α = 0.3, 自己=1.0, 朋友=weight, 陌生人=0
        end

        rect rgb(227, 242, 253)
            Note over RT: 3. 画像准备 + 日记溯源
            RT->>AS: get_persona_tags + get_persona_summary
            RT->>AS: 批量查 atoms_diary_links
            AS-->>RT: 关联 diary_id 列表
            RT->>DS: 批量查 diary_entries (IN 查询)
            DS-->>RT: diary content
            Note over RT: _pick_best_segment()<br/>关键词窗口 → 取命中句+前后文
        end

        RT-->>MC: RecallResult(memory_text, atoms, diary_refs)

        MC->>MJ: inject(memory_text, persona_text, system_prompt, user_message)
        MJ-->>MC: (modified_system, modified_user)
    end

    alt 消息被注入记忆
        MC-->>Plugin: 返回修改后的消息文本
        Plugin-->>LLM: 向 LLM 发送含记忆的上下文
    else 无记忆注入
        MC-->>Plugin: 返回 None
        Plugin-->>LLM: 原始上下文
    end
```

### 用户消息中的对话历史写入

```mermaid
sequenceDiagram
    participant Plugin as AstrBot Plugin
    participant CS as ConversationStore
    participant MC as MemoryCore
    participant CM as ConsolidationManager

    Note over Plugin: on_message（每条用户消息）
    Plugin->>CS: add_message(session_id, uid, user, content)
    Plugin->>CM: on_message(uid, text, name)
    CM->>CM: 更新 last_activity

    Note over Plugin: on_llm_response（LLM 回复后）
    Plugin->>CS: add_message(session_id, uid, assistant, response)
    Plugin->>CM: on_round_complete(uid, session_id)
    CM->>CM: msg_count++
```

---

## 三、后台整理流水线（异步路径）

```mermaid
sequenceDiagram
    participant CM as ConsolidationManager
    participant SS as StateStore
    participant CS as ConversationStore
    participant WP as WarmProcessor
    participant CP as Capturer
    participant MU as MemoryUnitOfWork
    participant AS as AtomStore+DiaryStore
    participant LLM as LLM
    participant GE as GraphEngine
    participant PE as PersonaEngine

    Note over CM: ─── 三种触发条件 ───
    
    alt A: on_round_complete → msg_count >= 10 轮
        CM->>CM: 轮数阈值触发
    else B: 空闲检测 → 60 分钟无活动
        CM->>CM: _idle_check_loop 触发
    else C: 定时扫描 → 每 120 分钟
        CM->>CM: _periodic_scan_loop 触发
    end

    Note over CM: 限速检查
    alt 全局限速(120s) or 用户限速(60s) 未通过
        Note over CM: 跳过本次触发
    else 通过限速
        CM->>CS: get_context_since(session_id, after_id, limit=50)
        CS-->>CM: 自上次整理后的新对话文本
        CM->>WP: enqueue(uid, text, state)
        WP->>WP: 放入 asyncio.Queue
    end

    Note over WP,PE: ─── 消费阶段（后台 Worker） ───

    WP->>WP: _worker_loop 取出任务
    WP->>WP: 用户级 60s 速率检查

    rect rgb(232, 245, 233)
        Note over WP: Step 1: Judge
        WP->>CP: should_capture(text)
        CP->>LLM: LLM 判断(值不值得记)
        LLM-->>CP: CaptureJudgeResult
        CP-->>WP: {should_remember, importance, mood, summary}
    end

    alt 不值得记
        WP->>CM: on_done(uid, result=wrote_diary=False)
        CM->>CM: 重置计数
    else 值得记
        Note over WP: Step 2: 提前去重
        WP->>CP: dedup_and_reinforce(text, threshold=0.85)
        alt 命中已有记忆
            Note over WP: 强化后跳过 Capture
            WP->>CM: on_done(uid, result=wrote_diary=False)
        else 新内容
            rect rgb(255, 243, 224)
                Note over WP: Step 3: Capture (合并模式)
                WP->>CP: capture(uid, text, judge)
                CP->>LLM: 一次调用 → 日记+原子
                LLM-->>CP: {diary: ..., atoms: [...]}

                Note over CP: 策略链加工
                CP->>CP: QualityCheckStep → AtomClassifyStep → DiaryFillStep → TruncateStep

                CP->>MU: append_diary(diary) → diary_entries
                CP->>MU: insert_atoms(atoms) → memory_atoms + FTS
                CP->>AS: link_atom_to_diary() → atoms_diary_links
                CP-->>WP: CaptureResult(wrote_diary=True, atoms=[...])
            end

            rect rgb(227, 242, 253)
                Note over CP,GE: 异步后处理 (fire-and-forget)
                CP-->>GE: callback index_diary(diary_id, content, entities)
                GE->>GS: 创建 diary/entity/user/topic 节点
                GE->>GS: 创建 mention/belongs_to 边
                GE->>GS: 增量更新 co_occur 边

                CP-->>CP: 异步计算 embedding
            end

            rect rgb(245, 245, 245)
                Note over WP: Step 4: 画像更新 (L3)
                alt diary_count_since_persona >= 10
                    WP->>PE: incremental_update(uid)
                    PE->>AS: 查旧画像 + 最近 5 篇日记 + 最近 10 条事实
                    PE->>LLM: LLM diff: 增量变化
                    LLM-->>PE: {add, modify, delete, tags}
                    PE->>AS: save_persona(uid, new_summary, tags)
                end
            end

            WP->>CM: on_done(uid, result)
            CM->>CM: reset_after_consolidation()
            CM->>SS: save_state(uid) [延迟刷写 5s]
        end
    end
```

---

## 四、三层画像流水线 (L1→L2→L3)

```mermaid
flowchart TB
    subgraph Input["输入"]
        I1["对话文本"]
        I2["用户消息"]
    end

    subgraph L1["L1: Raw Capture"]
        direction TB
        A1["对话整理\n(≥10轮/空闲超时/定时扫描)"] --> A2["Judge LLM\n值不值得记?"]
        A2 -->|值得| A3["提前去重\nJaccard ≥0.85?"]
        A3 -->|命中| A4["强化已有\n跳过 Capture"]
        A3 -->|新内容| A5["合并调用 LLM\n日记 + 原子 (一次调用)"]
        A5 --> A6["策略链加工\nQualityCheck → AtomClassify\n→ DiaryFill → Truncate"]
        A6 --> A7["日记入库\ndiary_entries"]
        A6 --> A8["原子入库\nmemory_atoms + FTS5"]
        A8 --> A9["桥表关联\natoms_diary_links"]
    end

    subgraph L2["L2: 图谱索引"]
        direction TB
        B1["回调: index_diary\n(diary_id, content, entities)"] --> B2["创建节点\ndiary / entity / user\ntopic / emotion / date"]
        B2 --> B3["创建边\nmention(实体→日记)\nbelongs_to(日记→元数据)\nco_occur(实体↔实体)"]
        B3 --> B4["可选: 实体 embedding"]
    end

    subgraph L3["L3: 画像引擎"]
        direction TB
        C1["条件: 距上次画像更新\n≥10 篇日记"] --> C2["查旧画像 + tags"]
        C2 --> C3["查最近 5 篇日记\n最近 10 条原子"]
        C3 --> C4["LLM 增量 diff"]
        C4 --> C5["应用: add/modify/delete\n+ 标签提取"]
        C5 --> C6["保存 user_persona\nsummary + tags + tier"]
    end

    I1 --> L1
    L1 -->|异步回调| L2
    L1 -->|diary_count ≥10| L3

    classDef input fill:#e8eaf6,stroke:#283593
    classDef l1 fill:#e8f5e9,stroke:#2e7d32
    classDef l2 fill:#fff3e0,stroke:#e65100
    classDef l3 fill:#e3f2fd,stroke:#1565c0
    class I1,I2 input
    class A1,A2,A3,A4,A5,A6,A7,A8,A9 l1
    class B1,B2,B3,B4 l2
    class C1,C2,C3,C4,C5,C6 l3
```

---

## 五、检索系统架构（双路四模式 + RRF 融合 + 社交加权重排序）

```mermaid
flowchart TB
    Q["用户查询"] --> KW["关键词提取<br/>jieba 分词 + 停用词过滤<br/>(最多 15 个关键词)"]

    KW --> BM25["BM25 文档路<br/>memory_atoms_fts (FTS5)<br/>candidate_k = 50"]
    KW --> GKW["Graph 关键词路<br/>nodes → edges → diary → fact<br/>candidate_k = 50"]
    KW --> VEC["向量语义路<br/>cosine similarity<br/>(可选, 需嵌入模型)"]
    KW --> GVEC["Graph 向量路<br/>节点 embedding 搜索<br/>(可选, 需嵌入模型)"]

    BM25 --> RRF["RRF 融合<br/>Reciprocal Rank Fusion<br/>k = 60"]
    GKW --> RRF
    VEC --> RRF
    GVEC --> RRF

    RRF --> FALLBACK["兜底补充<br/>全库 importance 降序<br/>(填补多路未覆盖)"]

    FALLBACK --> SOCIAL["社交加权重排序"]
    SOCIAL --> FRIEND["查 graph_edges<br/>friend_of 边 → weight"]

    SOCIAL --> FINAL["final_score = relevance × (1 + 0.3 × weight)<br/>自己=1.0 / 朋友=weight / 陌生人=0.0"]

    FINAL --> TOPK["取 top-k<br/>(默认 5 条)"]

    TOPK --> DIARY["日记回溯<br/>批量查 atoms_diary_links → diary_entries<br/>关键词上下文窗口选段"]
    TOPK --> PERSONA["画像装配<br/>tags + summary<br/>(bot 名 → 我)"]

    DIARY --> FMT["格式化输出"]
    PERSONA --> FMT

    FMT --> MEM["<<memory>><br/>【画像】标签 + 摘要<br/>【事实】原子列表<br/>【溯源】日记段落<br/>【提示】调用 search_memories</memory>"]

    classDef retrieve fill:#e3f2fd,stroke:#1565c0
    classDef fusion fill:#fff3e0,stroke:#e65100
    classDef social fill:#fce4ec,stroke:#c62828
    classDef output fill:#e8f5e9,stroke:#2e7d32
    classDef fallback fill:#f3e5f5,stroke:#4a148c
    class BM25,GKW,VEC,GVEC retrieve
    class RRF,FALLBACK,TOPK fusion
    class SOCIAL,FRIEND,FINAL social
    class DIARY,PERSONA,FMT fallback
    class MEM output
```

---

## 六、触发整理条件

```mermaid
flowchart LR
    subgraph Triggers["三种触发条件（任一满足）"]
        A["对话轮数阈值<br/>默认 10 轮<br/>(主触发)"]
        B["空闲超时<br/>默认 60 分钟<br/>(安全网)"]
        C["定时扫描<br/>默认 120 分钟<br/>(安全网)"]
    end

    Triggers --> OR{任一满足?}
    OR -->|是| RATE["限速检查"]

    subgraph RateLimit["限速 (全部通过才执行)"]
        D["去重间隔<br/>10s 内不重复检查"]
        E["用户级限速<br/>距上次 ≥ 60s"]
        F["全局限速<br/>距上次 ≥ 120s"]
    end

    RATE --> RateLimit
    RateLimit -->|通过| WP["WarmProcessor 入队列"]
    RateLimit -->|未通过| SKIP["跳过本次"]

    WP --> P["并发限速<br/>60s/用户<br/>(队列内)"]
    P --> WORK["Worker 消费<br/>Judge → Capture → Persona"]

    classDef trigger fill:#fce4ec,stroke:#c62828
    classDef rate fill:#fff8e1,stroke:#f57f17
    classDef flow fill:#e8f5e9,stroke:#2e7d32
    class A,B,C trigger
    class D,E,F,P rate
    class SKIP skip
```

---

## 七、数据表关系（核心模型）

```mermaid
erDiagram
    canonical_users ||--o{ user_identities : "一个用户有多个平台身份"
    canonical_users ||--o| user_persona : "一个用户有一个画像"
    canonical_users ||--o{ diary_entries : "一个用户有多篇日记"

    diary_entries ||--o{ atoms_diary_links : "一篇日记关联多个原子"
    memory_atoms ||--o{ atoms_diary_links : "一个原子被多篇日记引用"
    memory_atoms ||--o| memory_atoms_fts : "FTS5 索引"

    atomic_facts ||--o{ diary_fact_links : "一个事实被多篇日记引用"
    diary_entries ||--o{ diary_fact_links : "一篇日记有多个事实"

    %% graph.db
    nodes ||--o{ edges : "节点间有边"
    edges ||--o| diary_entries : "边可通过 diary_id 关联到日记"

    canonical_users {
        string uid PK "u_xxxx"
        string primary_name "显示名"
    }
    user_identities {
        string platform_id PK "qq:12345"
        string uid FK "关联 canonical_users"
        string platform "qq / telegram"
        string display_name "显示名"
    }
    user_persona {
        string uid PK
        string summary "一句话摘要"
        string full_markdown "完整画像"
        string tags '["技术","Python"]'
        string tier "new / active / core"
        int incremental_count "增量次数"
        int diary_count_since_full "距上次全量日记数"
    }
    diary_entries {
        int id PK
        string user_id
        string date "YYYY-MM-DD"
        string content "日记正文(frontmatter + body)"
        float importance
        string sentiment
        string topics
    }
    memory_atoms {
        int id PK
        string user_id
        string diary_date
        string atom_type "episodic / factual / preference / planned / relational"
        string content "<= 200 字"
        float importance
        float confidence
        string entities '["实体名"]'
        blob embedding "向量 JSON"
        string status "active / dormant / archived / forgotten"
        float ttl_days "TTL 天数"
        float expires_at "过期时间戳"
        string decay_type "exponential / linear / step"
    }
    memory_atoms_fts {
        int atom_id FK
        string content
        string user_id
    }
    atoms_diary_links {
        int atom_id FK
        int diary_id FK
        float importance
        string snippet "原文片段"
    }
    atomic_facts {
        int id PK
        string content UNIQUE "全局去重"
        int source_count "引用次数"
        float importance
    }
    diary_fact_links {
        int diary_id FK
        int fact_id FK
        float importance
        string snippet
    }
    nodes {
        string id PK "entity:hako / user:u_xxx / topic:coffee"
        string type "entity / user / topic / emotion / date / diary"
        string name
        blob embedding
    }
    edges {
        string id PK
        string from_node FK
        string to_node FK
        string relation_type "mention / co_occur / belongs_to / friend_of / blocked_by"
        int diary_id
        float weight
        string status "active / pending / passive / blocked / rejected"
    }
```

---

## 八、后台定时循环

| 循环 | 周期 | 职责 | 文件 |
|------|------|------|------|
| **对话滑动窗口清理** | 每 120s | 清理 `conversations.db` 过期的消息记录 | `memory_core.py:_cleanup_loop()` |
| **图谱共现统计** | 每 24h | 增量统计 co_occur 边权重 | `graph_engine.py:batch_cooccur()` |
| **重要性衰减** | 每 24h | importance × 0.99 (按原子类型不同半衰期) | `lifecycle/decay.py` |
| **归档** | 每 24h | 旧日记转 Markdown 文件输出 | `lifecycle/archiver.py` |
| **清理** | 每 24h | 删除孤立原子、过期 `forgotten` 原子 | `lifecycle/cleanup.py` |
| **会话状态刷写** | 每 5s | 延迟写入 `consolidation_state` | `consolidation_manager.py:_flush_loop()` |
| **空闲检测** | 每 60s | 扫描无活动用户，触发兜底整理 | `consolidation_manager.py:_idle_check_loop()` |
| **定时扫描** | 每 120 分钟 | 全量扫描积压用户 | `consolidation_manager.py:_periodic_scan_loop()` |

---

## 九、模块职责说明

| 模块 | 路径 | 职责 |
|------|------|------|
| **MemoryCore** | `core/memory_core.py` | 统一门面，初始化 8 阶段装配，对外暴露 `process_message()` |
| **MemoryInjector** | `core/memory_injector.py` | 控制注入位置(system_prompt/user_message/knowledge/manual)和模板 |
| **Retriever** | `core/retriever.py` | 双路四模式检索+RRF融合+社交加权重排序，批量日记溯源 |
| **GraphEngine** | `features/graph_engine.py` | Capturer 回调索引日记 → 节点/边，社交关系 claim/confirm/reject 管理 |
| **PersonaEngine** | `features/persona_engine.py` | 增量(LLM diff) / 全量(LLM rebuild) 画像更新，60s TTLCache |
| **ConsolidationManager** | `pipeline/consolidation_manager.py` | 三种触发 + 两级限速 + 5s延迟刷写状态 |
| **WarmProcessor** | `pipeline/warm_processor.py` | 异步队列消费者(3重试+退避)，提前去重→Capture→Persona |
| **Capturer** | `pipeline/capturer.py` | Judge+合并模式Capture+策略链+异步图谱索引+异步embedding |
| **CommandHandler** | `features/command_handler.py` | `/日记 /记忆 /记忆搜索 /记忆删除 /记忆统计 /记忆重构` |
| **MemoryUnitOfWork** | `pipeline/memory_uow.py` | Capturer的存储门面，封装 DiaryStore+AtomStore+WriteOpLog |

### 与旧版（已删除）的关键差异

| 特性 | 旧版 (`core/`) | 新版 (`memori/core/`) |
|------|---------------|----------------------|
| HotMessageCache | 有 (WAL + 60s刷写) | **已删除**，消息直接写 `conversation_store.add_message()` |
| 检索范围 | 按 user_id 过滤 | **全局检索 + 社交加权重排序** |
| 日记回溯 | 逐条查询 `_find_atom_diaries()` → `_best_diary_segment()` | **批量查询** `atoms_diary_links` → `IN(...)` 批量读 diary |
| 段落匹配 | Jaccard 相似度 | **关键词上下文窗口** (取命中句+前后句) |
| 社交排序 | 无 | **`_social_rerank()`** friend_of 边加权 |
| 嵌入模型初始化 | 从外部传入 | **`_init_embed_provider()`** 从配置动态创建 |
| 后台清理循环 | HotCache 刷写 | **`_cleanup_loop()`** 对话滑动窗口清理 |
| 后端任务 | `hotcache_flush` + `co_occur` + `lifecycle` | `cleanup`(120s) + `co_occur`(24h) + `lifecycle`(24h) |
| Embedding 支持 | 固定 | bge-m3 / Ollama / API 三种后端动态切换 |
