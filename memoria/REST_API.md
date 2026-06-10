# Memoria REST API 规划 v0.1

> 当 memoria 作为独立 HTTP 服务运行时提供的接口规划。
> 当前阶段通过 AstrBot 插件桥接，此文档为未来独立部署的蓝图。

---

## 设计原则

1. **Stateless** — 服务无状态，认证由上层代理（API Key / OAuth）处理
2. **Event-Driven** — 核心入口是 `POST /v1/events`，其他为管理/查询接口
3. **Async** — 所有端点异步，长操作返回 `202 Accepted` + 轮询 ID
4. **Versioned** — URL 前缀 `/v1/`

---

## 接口清单

### 核心 — 记忆处理

```
POST /v1/events              # 提交消息事件 → 召回记忆 + 注入上下文
POST /v1/events/async        # 提交事件 → 异步处理（回调解耦）
GET  /v1/events/{id}/status  # 查询异步任务状态
```

### 检索 — 记忆查询

```
GET    /v1/memories           # 按关键词检索记忆
GET    /v1/memories/{id}      # 获取单条记忆详情
PUT    /v1/memories/{id}      # 更新记忆
DELETE /v1/memories/{id}      # 删除记忆
GET    /v1/memories/timeline  # 按时间线浏览
```

### 日记

```
GET    /v1/diaries            # 日记列表（分页）
GET    /v1/diaries/{date}     # 指定日期日记
PUT    /v1/diaries/{date}     # 编辑日记
```

### 图谱

```
GET    /v1/graph/nodes        # 图节点列表
GET    /v1/graph/edges        # 图边列表
GET    /v1/graph/query        # 实体邻居查询
```

### 用户

```
GET    /v1/users              # 用户列表
GET    /v1/users/{uid}        # 用户详情 + 画像
GET    /v1/users/{uid}/persona # 用户画像
```

### 系统

```
GET  /v1/stats                # 系统统计
POST /v1/archive/run          # 手动触发归档
POST /v1/decay/run            # 手动触发衰减
```

---

## 请求/响应格式

### POST /v1/events

```json
{
    "user_id": "2398604399",
    "text": "今天测试辛苦了，我给你带好吃的",
    "sender_name": "Hako",
    "session_id": "sess_abc123",
    "system_prompt": "你是Hana，一个可爱的AI助手"
}
```

**响应**（同步模式）：

```json
{
    "ok": true,
    "data": {
        "modified_text": "（可能被记忆注入修改后的用户消息）",
        "injected_memories": [
            {"type": "episodic", "content": "Hako主动提出带好吃的", "date": "2026-06-10"}
        ],
        "persona": "Hako是一个技术爱好者，喜欢探索AI...",
        "recalled_count": 3
    }
}
```

### GET /v1/memories?q=关键词&uid=用户&k=5

```json
{
    "ok": true,
    "data": {
        "results": [
            {"content": "Hako喜欢草莓味冰淇淋", "type": "preference",
             "importance": 0.7, "date": "2026-06-08", "diary_snippet": "..."}
        ],
        "total": 1
    }
}
```

---

## 未来扩展

- **WebSocket** `/v1/ws` — 实时事件流，用于聊天类集成
- **批量导入** `POST /v1/import` — 从 JSON/Markdown 批量导入记忆
- **SSE** `GET /v1/events/stream` — Server-Sent Events 推送记忆变化
