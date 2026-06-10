# memori 配置项参考

所有配置项通过 `config` 字典传入 `MemoryCore(config={...})`。

---

## 一、基础

| 键 | 类型 | 默认值 | 说明 |
|---|------|--------|------|
| `bot_name` | str | `"Hana"` | Bot 显示名，写入身份表 |
| `data_dir` | str | `./data` | 数据库存放目录 |
| `archive.enabled` | bool | `True` | 启用冷存储归档 |
| `archive.path` | str | `"./memory_archive"` | 归档文件输出目录 |

## 二、检索 — 记忆召回

| 键 | 类型 | 默认值 | 说明 |
|---|------|--------|------|
| `recall_count` | int | `5` | 每次召回最多多少条记忆原子 |
| `recall_max_tokens` | int | `500` | 召回文本的 token 上限（≈2000 字） |

硬编码常量（不可配）：
- `RRF_K = 60` — RRF 融合常数，越大跨列表排名影响越小

## 三、注入 — 记忆放入 LLM 上下文

| 键 | 类型 | 默认值 | 说明 |
|---|------|--------|------|
| `injection_position` | str | `"system_prompt_suffix"` | 注入位置，可选值：<br>`system_prompt_suffix` — 系统提示词末尾<br>`user_message_prefix` — 用户消息之前<br>`user_message_suffix` — 用户消息之后<br>`knowledge_section` — 知识库区域<br>`manual_only` — 不自动注入 |
| `injection_template` | str | `""` | 自定义模板，`{{content}}` 代表记忆内容，`{{user}}` 代表用户名。为空时使用内置模板 |
| `injection_use_tag` | bool | `True` | 是否用 `<memory>` 标签包裹记忆块 |

## 四、整理触发 — 什么时候写日记

| 键 | 类型 | 默认值 | 说明 |
|---|------|--------|------|
| `trigger_msg_count` | int | `10` | 累计多少条消息后触发一次整理 |
| `trigger_time_minutes` | int | `360` | 距上次整理超过多少分钟触发 |
| `warmup_enabled` | bool | `True` | 暖启动：前几次整理的阈值指数增长 |
| `idle_timeout_minutes` | int | `30` | 用户闲置超过此时间后触发整理 |

硬编码常量（不可配）：
- `_debounce_interval = 10.0` 秒 — 同一用户两次触发判断的最小间隔
- `_min_global_interval = 120.0` 秒 — 全局限速，两次整理至少间隔 2 分钟

## 五、后台整理（WarmProcessor）

| 键 | 类型 | 默认值 | 说明 |
|---|------|--------|------|
| `max_l1_retries` | int | `3` | 写日记/提取原子失败时最多重试次数 |
| `persona_update_interval` | int | `10` | 每 N 篇日记更新一次用户画像 |

硬编码常量（不可配）：
- `_min_user_interval = 60.0` 秒 — 同一用户两次后台处理的最小间隔

## 六、日记与原子提取

| 键 | 类型 | 默认值 | 说明 |
|---|------|--------|------|
| `max_diary_tokens` | int | `500` | LLM 写日记的 token 上限 |

## 七、重要度衰减

| 键 | 类型 | 默认值 | 说明 |
|---|------|--------|------|
| `decay_rate` | float | `0.99` | 每日衰减系数（每天 `importance *= 0.99`） |
| `decay_enabled` | bool | `True` | 是否启用衰减 |
| `expired_atom_ttl_days` | int | `60` | 被遗忘的原子超过此天数后永久删除 |

## 八、预过滤（新用户降噪）

| 键 | 类型 | 默认值 | 说明 |
|---|------|--------|------|
| `pre_filter_enabled` | bool | `False` | 是否启用预过滤（新用户消息过滤） |

预过滤规则（硬编码）：
- 长度 `< 3` 字 → 丢弃
- 重复率 `> 60%` → 丢弃
- 纯 emoji → 丢弃
- 含关键词 → 放行

## 九、热缓存

| 键 | 类型 | 默认值 | 说明 |
|---|------|--------|------|
| `hot_cache_size` | int | `20` | 硬编码 `MAX_PER_USER = 20`，每用户最多缓存 20 条最近消息 |

## 十、冷存储归档

| 键 | 类型 | 默认值 | 说明 |
|---|------|--------|------|
| `warm_days` | int | `90` | 超过此天数的日记进入「温」状态 |
| `cold_importance_threshold` | float | `0.1` | 重要度低于此值的日记可归档 |
| `max_summary_chars` | int | `200` | 归档摘要最多字数 |

---

## 快速参考：建议的配置模板

```python
config = {
    # Bot
    "bot_name": "Hana",

    # 检索
    "recall_count": 5,
    "recall_max_tokens": 500,

    # 注入
    "injection_position": "system_prompt_suffix",
    "injection_use_tag": True,

    # 整理触发
    "trigger_msg_count": 10,
    "trigger_time_minutes": 360,     # 6 小时
    "warmup_enabled": True,
    "idle_timeout_minutes": 30,

    # 后台
    "max_l1_retries": 3,
    "persona_update_interval": 10,

    # 衰减
    "decay_rate": 0.99,
    "decay_enabled": True,
    "expired_atom_ttl_days": 60,

    # 归档
    "archive": {
        "enabled": True,
        "path": "./memory_archive",
    },
}
```

> 共计 **20 个可配项**（`config.get()`）+ 4 个位置选项（`injection_position` 的枚举值）+ 5 个硬编码常量（可通过改源码调整）。
