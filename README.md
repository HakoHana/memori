# Hana Memory — 日记式长期记忆插件

让 AstrBot 拥有**长期记忆**。日记驱动、原子事实 + 知识图谱双路检索、用户画像沉淀。

## 功能

- **📝 日记式记忆** — LLM 以第一人称写日记，记录对话中的重要时刻
- **🔍 原子事实** — 从日记提取结构化事实（episodic / factual / preference / planned / relational），支持 FTS5 全文搜索
- **🕸️ 知识图谱** — 自动构建实体关联图，可视化浏览记忆关联
- **🧠 用户画像** — 长期沉淀用户特征，随对话增量更新
- **🔀 双路检索 + RRF 融合** — BM25 文档路 + GraphEntity 图路 → Reciprocal Rank Fusion
- **🎯 jieba 中文分词** — 精确词级匹配，告别字符 2-gram 噪音
- **🆔 多平台身份映射** — 内部 UID 流转，LLM 只看昵称，不暴露平台 ID
- **📊 WebUI Dashboard** — 图谱可视化、记忆管理、日记编辑
- **⚡ 异步后台处理** — 整理/建图/画像全后台，不阻塞实时回复

## 架构速览

```
消息到达 → HotCache(零I/O) → 记忆召回 → LLM 回复
                                  │
                          后台队列处理
                     Judge → Capture → 图谱 → 画像
```

**数据分层**：

| 层 | 存储 | 来源 |
|----|------|------|
| L0 | `conversation_store` | 原始对话消息 |
| L1 | `diary_entries` | LLM 写的日记 |
| L2 | `atomic_facts` | 从日记提取的结构化事实 |
| L3 | `user_persona` | 用户特征画像 |
| L4 | `graph_nodes/edges` | 实体关联图谱 |

## 快速开始

### 安装

将插件目录放入 AstrBot 的 `data/plugins/`：

```bash
cd data/plugins/
git clone https://github.com/HakoHana/astrbot_plugin_memory.git
```

在 AstrBot WebUI 中重载插件。依赖自动安装（`requirements.txt`）。

### 依赖

- `aiosqlite>=0.19.0` — 异步 SQLite
- `cachetools>=7.1.0` — TTL 缓存
- `jieba>=0.42.1` — 中文分词

### 配置

在 WebUI 插件配置页可配置：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `bot_name` | `Hana` | Bot 显示名 |
| `recall_count` | `5` | 每次召回原子数 |
| `trigger_msg_count` | `10` | 整理触发消息数阈值 |
| `trigger_time_minutes` | `360` | 整理触发时间间隔（分钟） |
| `injection_position` | `system_prompt_suffix` | 记忆注入位置 |
| `pre_filter_enabled` | `false` | 新用户预过滤开关 |

## 数据文件

```
data/plugin_data/astrbot_plugin_memory/
├── memory.db              # 主数据库（含 FTS5 全文索引）
├── memory_archive/        # 冷存储归档目录
│   └── {uid}/YYYY/MM/     # 按用户/年/月归档
└── personas/              # 旧版画像文件（迁移中）
```

## 命令

| 命令 | 说明 |
|------|------|
| `/日记` | 查看最近日记 |
| `/日记列表` | 日记日期列表 |
| `/记忆` | 查看记忆统计 |
| `/记忆搜索 <关键词>` | 搜索记忆 |
| `/记忆删除 <id>` | 删除指定记忆 |
| `/记忆重构` | 逐条重建旧记忆 |

## 版本历史

| 版本 | 日期 | 关键变更 |
|------|------|---------|
| v0.3.0 | 2026-06-09 | L0-L4 分层、双路检索、WarmProcessor、身份体系 |
| v0.3.1 | 2026-06-10 | jieba 分词替换 2-gram；完整对话传参修复身份归因；Bot 消息 `[Bot: Hana]:` 格式 |

## 项目结构

```
astrbot_plugin_memory/
├── main.py                    # 插件入口
├── metadata.yaml              # AstrBot 插件元数据
├── requirements.txt           # 依赖
├── core/
│   ├── memory_core.py         # 核心协调层
│   ├── retriever.py           # 检索引擎（jieba→DualRoute→RRF）
│   ├── retrieval/             # 双路检索子模块
│   │   ├── dual_route_retriever.py
│   │   ├── bm25_retriever.py
│   │   ├── graph_entity_retriever.py
│   │   └── rrf_fusion.py
│   ├── capturer.py            # Judge + 写日记 + 提取原子
│   ├── warm_processor.py      # 异步队列消费者
│   ├── consolidation_manager.py  # 消息计数 + 触发判断
│   ├── memory_injector.py     # 记忆注入 LLM 上下文
│   ├── persona_engine.py      # 用户画像引擎
│   ├── graph_engine.py        # 知识图谱引擎
│   ├── hot_cache.py           # 内存热缓存
│   ├── archiver.py            # 冷存储归档
│   └── command_handler.py     # 指令处理器
├── storage/                   # 存储层
│   ├── base_store.py          # SQLite 连接池
│   ├── atom_store.py          # 原子 + 身份 + FTS5
│   ├── diary_store.py         # 日记
│   ├── conversation_store.py  # 原始对话
│   ├── graph_store.py         # 图谱
│   ├── persona_store.py       # 画像
│   └── state_store.py         # 状态追踪
├── models/                    # 数据模型
├── prompts/                   # LLM 提示词模板
└── webui/                     # 前端页面
```

## License

MIT
