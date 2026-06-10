"""FastAPI 路由 — 所有 RESTful 端点"""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..core.memory_core import MemoryCore
from .deps import get_core
from .schemas import *

router = APIRouter()


# ═══════════════════════════════════════════════════════════
#  事件处理
# ═══════════════════════════════════════════════════════════

@router.post("/v1/events", response_model=EventResponse)
async def process_event(body: EventRequest, core: MemoryCore = Depends(get_core)):
    """提交消息 → 召回记忆 → 注入上下文"""
    modified = await core.process_message(
        user_id=body.user_id,
        message_text=body.text,
        sender_name=body.sender_name,
        system_prompt=body.system_prompt,
    )

    # 后台触发整理（不阻塞响应）
    await core.trigger_capture(body.user_id, body.text)

    # 召回结果
    recall = await core.retriever.get_context_memories(body.user_id, body.text)

    return EventResponse(
        modified_text=modified,
        injected_count=len(recall.atoms),
        recalled_count=len(recall.atoms),
    )


# ═══════════════════════════════════════════════════════════
#  记忆检索
# ═══════════════════════════════════════════════════════════

@router.get("/v1/memories", response_model=MemorySearchResult)
async def search_memories(
    q: str = Query("", description="搜索关键词"),
    uid: str = Query("", description="用户 ID"),
    k: int = Query(5, ge=1, le=50),
    core: MemoryCore = Depends(get_core),
):
    """按关键词检索记忆"""
    if not q or not uid:
        return MemorySearchResult(results=[], total=0)

    atoms = await core.retriever.recall(uid, q, k)
    results = [
        MemoryAtomOut(id=a.atom_id, content=a.content, atom_type=a.atom_type.value,
                      importance=a.importance, diary_date=a.diary_date)
        for a in atoms
    ]
    return MemorySearchResult(results=results, total=len(results))


@router.get("/v1/memories/{memory_id}", response_model=dict)
async def get_memory_detail(memory_id: int, core: MemoryCore = Depends(get_core)):
    """获取单条记忆详情"""
    # 先查 diary
    row = await core.atom_store.fetchone(
        "SELECT * FROM diary_entries WHERE id=?", (memory_id,)
    )
    if not row:
        raise HTTPException(404, "记忆不存在")

    columns = ["id", "uid", "user_id", "date", "timestamp", "content", "importance",
               "mood", "topics", "sentiment", "fact_extracted", "fact_retry_count",
               "archived", "correction", "created_at"]
    diary = dict(zip(columns, row))

    # 关联原子
    atoms = await core.atom_store.fetch(
        "SELECT id, content, atom_type, importance FROM memory_atoms WHERE diary_id=? AND status='active' ORDER BY importance DESC",
        (memory_id,),
    )
    diary["atoms"] = [
        {"id": a[0], "content": a[1], "type": a[2], "importance": a[3]}
        for a in atoms
    ]
    return diary


@router.put("/v1/memories/{memory_id}")
async def update_memory(memory_id: int, body: MemoryUpdateRequest,
                        core: MemoryCore = Depends(get_core)):
    """更新记忆"""
    updates = {k: v for k, v in body.model_dump(exclude_none=True).items()
               if k in ("content", "importance", "status")}
    if not updates:
        raise HTTPException(400, "没有可更新的字段")
    updates["updated_at"] = time.time()
    sets = ", ".join(f"{k}=?" for k in updates)
    vals = list(updates.values()) + [memory_id]
    await core.atom_store.execute(f"UPDATE diary_entries SET {sets} WHERE id=?", vals)
    return {"ok": True}


@router.delete("/v1/memories/{memory_id}")
async def delete_memory(memory_id: int, core: MemoryCore = Depends(get_core)):
    """删除记忆"""
    # 清理独占原子
    exclusive = await core.atom_store.fetch("""
        SELECT ma.id FROM memory_atoms ma
        WHERE ma.diary_id=? AND ma.status='active'
        AND (SELECT COUNT(*) FROM memory_atoms sub
             WHERE sub.content=ma.content AND sub.user_id=ma.user_id
             AND sub.status='active' AND sub.diary_id!=ma.diary_id) = 0
    """, (memory_id,))
    eids = [r[0] for r in exclusive]
    if eids:
        ph = ",".join("?" * len(eids))
        await core.atom_store.execute(
            f"UPDATE memory_atoms SET status='forgotten' WHERE id IN ({ph})", eids
        )
    await core.atom_store.execute("DELETE FROM diary_entries WHERE id=?", (memory_id,))
    return {"ok": True, "cleaned_atoms": len(eids)}


@router.get("/v1/memories/timeline")
async def get_timeline(
    uid: str = Query(..., description="用户 ID"),
    year: str = Query("", description="年份过滤"),
    month: str = Query("", description="月份过滤"),
    core: MemoryCore = Depends(get_core),
):
    """按时间线浏览记忆日期"""
    if year and month:
        ym = f"{year}-{int(month):02d}"
        rows = await core.atom_store.fetch(
            "SELECT DISTINCT date FROM diary_entries WHERE user_id=? AND date LIKE ? ORDER BY date DESC",
            (uid, f"{ym}%"),
        )
    elif year:
        rows = await core.atom_store.fetch(
            "SELECT DISTINCT date FROM diary_entries WHERE user_id=? AND date LIKE ? ORDER BY date DESC",
            (uid, f"{year}%"),
        )
    else:
        rows = await core.atom_store.fetch(
            "SELECT DISTINCT date FROM diary_entries WHERE user_id=? ORDER BY date DESC LIMIT 100",
            (uid,),
        )
    return {"ok": True, "dates": [r[0] for r in rows]}


# ═══════════════════════════════════════════════════════════
#  日记
# ═══════════════════════════════════════════════════════════

@router.get("/v1/diaries")
async def list_diaries(
    uid: str = Query("", description="用户 ID（空=全部）"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    core: MemoryCore = Depends(get_core),
):
    """日记列表（分页）"""
    offset = (page - 1) * size
    if uid:
        rows = await core.atom_store.fetch(
            "SELECT id, user_id, date, importance, sentiment, topics FROM diary_entries WHERE user_id=? ORDER BY date DESC LIMIT ? OFFSET ?",
            (uid, size, offset),
        )
        total = (await core.atom_store.fetchone(
            "SELECT COUNT(*) FROM diary_entries WHERE user_id=?", (uid,)
        ))[0]
    else:
        rows = await core.atom_store.fetch(
            "SELECT id, user_id, date, importance, sentiment, topics FROM diary_entries ORDER BY date DESC LIMIT ? OFFSET ?",
            (size, offset),
        )
        total = (await core.atom_store.fetchone("SELECT COUNT(*) FROM diary_entries"))[0]
    items = []
    for r in rows:
        topics_raw = r[5]
        topics = []
        if topics_raw:
            try:
                topics = json.loads(topics_raw) if isinstance(topics_raw, str) else topics_raw
            except Exception:
                topics = [str(topics_raw)]
        items.append({
            "id": r[0], "user_id": r[1], "date": r[2],
            "importance": r[3], "sentiment": r[4], "topics": topics,
        })
    return {"ok": True, "items": items, "total": total, "page": page, "size": size}


@router.get("/v1/diaries/{date}")
async def get_diary(
    date: str,
    uid: str = Query(..., description="用户 ID"),
    core: MemoryCore = Depends(get_core),
):
    """获取指定日期日记"""
    row = await core.atom_store.fetchone(
        "SELECT * FROM diary_entries WHERE user_id=? AND date=? ORDER BY id DESC LIMIT 1",
        (uid, date),
    )
    if not row:
        raise HTTPException(404, "该日期没有日记")
    columns = ["id", "uid", "user_id", "date", "timestamp", "content", "importance",
               "mood", "topics", "sentiment", "fact_extracted", "fact_retry_count",
               "archived", "correction", "created_at"]
    diary = dict(zip(columns, row))
    return {"ok": True, "data": diary}


@router.put("/v1/diaries/{date}")
async def update_diary(
    date: str,
    body: DiaryUpdateRequest,
    uid: str = Query(..., description="用户 ID"),
    core: MemoryCore = Depends(get_core),
):
    """更新指定日期日记"""
    from ..core.diary_helper import parse_diary_content, mood_to_sentiment
    content = body.content
    await core.diary_store.upsert(uid, date, content)
    fm, _ = parse_diary_content(content)
    updates = {}
    if "mood" in fm:
        updates["sentiment"] = mood_to_sentiment(str(fm["mood"]))
    if "importance" in fm:
        updates["importance"] = float(fm["importance"])
    if "topics" in fm:
        topics = fm["topics"]
        if isinstance(topics, list):
            updates["topics"] = json.dumps(topics, ensure_ascii=False)
    if updates:
        await core.diary_store.update_metadata(uid, date, **updates)
    return {"ok": True}


# ═══════════════════════════════════════════════════════════
#  图谱
# ═══════════════════════════════════════════════════════════

@router.get("/v1/graph/overview")
async def graph_overview(core: MemoryCore = Depends(get_core)):
    """图谱概览"""
    nodes = await core.atom_store.fetch(
        "SELECT node_type, COUNT(*) FROM graph_nodes GROUP BY node_type"
    )
    edges = await core.atom_store.fetch(
        "SELECT relation_type, COUNT(*) FROM graph_edges GROUP BY relation_type"
    )
    return {
        "ok": True,
        "nodes": {r[0]: r[1] for r in nodes},
        "edges": {r[0]: r[1] for r in edges},
    }


@router.post("/v1/graph/query")
async def graph_query(body: GraphQueryRequest, core: MemoryCore = Depends(get_core)):
    """实体邻居查询"""
    from ..core.graph_engine import GraphEngine
    ge = GraphEngine(
        graph_store=core.graph_store,
        atom_store=core.atom_store,
        diary_store=core.diary_store,
    )
    result = await ge.query_neighbors(body.entity)
    return {
        "ok": True,
        "nodes": [
            {"id": n.node_key, "type": n.node_type, "label": n.value}
            for n in result.get("nodes", [])
        ],
        "edges": [
            {"source": e.source, "target": e.target, "label": e.relation_type}
            for e in result.get("edges", [])
        ],
    }


# ═══════════════════════════════════════════════════════════
#  用户
# ═══════════════════════════════════════════════════════════

@router.get("/v1/users")
async def list_users(core: MemoryCore = Depends(get_core)):
    """用户列表"""
    rows = await core.atom_store.fetch("""
        SELECT cp.uid, cp.primary_name, up.tier, up.summary, up.last_full_update
        FROM canonical_users cp
        LEFT JOIN user_persona up ON cp.uid = up.uid
        ORDER BY up.last_full_update DESC
    """)
    users = [
        {"uid": r[0], "name": r[1] or r[0], "tier": r[2] or "new",
         "summary": (r[3] or "")[:100], "last_active": r[4]}
        for r in rows
    ]
    return {"ok": True, "users": users}


@router.get("/v1/users/{uid}")
async def get_user_detail(uid: str, core: MemoryCore = Depends(get_core)):
    """用户详情"""
    row = await core.atom_store.fetchone("SELECT * FROM user_persona WHERE uid=?", (uid,))
    if not row:
        raise HTTPException(404, "用户不存在")
    cols = ["uid", "summary", "full_markdown", "tags", "version", "tier",
            "last_full_update", "last_incremental_update", "known_ids", "primary_name",
            "identity_confidence", "incremental_count", "diary_count_since_full",
            "created_at", "updated_at"]
    return {"ok": True, "data": dict(zip(cols, row))}


@router.get("/v1/users/{uid}/persona")
async def get_persona(uid: str, core: MemoryCore = Depends(get_core)):
    """获取用户画像"""
    row = await core.atom_store.fetchone(
        "SELECT summary, full_markdown, tags FROM user_persona WHERE uid=?", (uid,)
    )
    if not row:
        return {"ok": True, "summary": "", "full_markdown": "", "tags": []}
    tags = []
    if row[2]:
        try:
            tags = json.loads(row[2]) if isinstance(row[2], str) else row[2]
        except Exception:
            tags = []
    return {"ok": True, "summary": row[0] or "", "full_markdown": row[1] or "", "tags": tags}


# ═══════════════════════════════════════════════════════════
#  系统
# ═══════════════════════════════════════════════════════════

@router.get("/v1/stats")
async def get_stats(core: MemoryCore = Depends(get_core)):
    """系统统计"""
    return {
        "ok": True,
        "users": (await core.atom_store.fetchone("SELECT COUNT(DISTINCT uid) FROM user_persona"))[0] or 0,
        "diaries": (await core.atom_store.fetchone("SELECT COUNT(*) FROM diary_entries"))[0] or 0,
        "atoms": (await core.atom_store.fetchone("SELECT COUNT(*) FROM memory_atoms WHERE status='active'"))[0] or 0,
        "facts": (await core.atom_store.fetchone("SELECT COUNT(*) FROM atomic_facts"))[0] or 0,
        "graph_nodes": (await core.atom_store.fetchone("SELECT COUNT(*) FROM graph_nodes"))[0] or 0,
        "graph_edges": (await core.atom_store.fetchone("SELECT COUNT(*) FROM graph_edges"))[0] or 0,
    }


@router.post("/v1/archive/run")
async def trigger_archive(core: MemoryCore = Depends(get_core)):
    """手动触发归档"""
    if not hasattr(core, 'archiver') or not core.archiver:
        raise HTTPException(400, "归档模块未启用")
    archived = await core.archiver.archive_daily()
    return {"ok": True, "archived": archived}


@router.post("/v1/decay/run")
async def trigger_decay(core: MemoryCore = Depends(get_core)):
    """手动触发重要度衰减"""
    rate = float(core.config.get("decay_rate", 0.99))
    enabled = core.config.get("decay_enabled", True)
    if not enabled:
        raise HTTPException(400, "衰减已禁用")
    await core.atom_store.apply_decay(rate)
    await core.atom_store.execute(
        f"UPDATE atomic_facts SET importance = importance * {rate} WHERE importance > 0.1"
    )
    return {"ok": True, "decay_rate": rate}


# ═══════════════════════════════════════════════════════════
#  配置
# ═══════════════════════════════════════════════════════════

_CONFIG_META = {
    "bot_name": {"type": "string", "default": "Hana", "label": "Bot 名称", "group": "基础",
                 "hint": "在对话和记忆中使用的名称"},
    "llm_provider_id": {"type": "string", "default": "", "label": "主模型", "group": "基础",
                        "hint": "用于记忆整理（写日记/提取原子）的 LLM 配置名。在下方「模型提供商」中配置"},
    "judge_provider_id": {"type": "string", "default": "", "label": "判读模型", "group": "基础",
                          "hint": "用于判断值不值得记的 LLM，需已在模型提供商中配置。留空 = 与主模型相同"},
    "recall_count": {"type": "int", "default": 5, "label": "召回条数", "group": "检索",
                     "hint": "每次消息处理时最多召回多少条记忆原子"},
    "recall_max_tokens": {"type": "int", "default": 500, "label": "召回 token 上限", "group": "检索",
                          "hint": "召回的文本总长度限制，超过则截断（约 2000 汉字）"},
    "injection_position": {
        "type": "select", "default": "system_prompt_suffix",
        "options": ["system_prompt_suffix", "user_message_prefix", "user_message_suffix", "knowledge_section", "manual_only"],
        "label": "记忆注入位置", "group": "注入",
        "hint": "记忆文本插入到 LLM 上下文的哪个位置",
    },
    "injection_use_tag": {"type": "bool", "default": True, "label": "启用 <memory> 标签包裹", "group": "注入",
                          "hint": "用 <memory>...</memory> 标签标记记忆内容，方便 LLM 识别"},
    "injection_template": {"type": "text", "default": "", "label": "注入自定义模板", "group": "注入",
                           "hint": "{{content}} = 记忆内容, {{user}} = 用户名。为空则使用内置模板"},
    "pre_filter_enabled": {"type": "bool", "default": False, "label": "预过滤（新用户降噪）", "group": "注入",
                           "hint": "启用后新用户短消息/重复消息/纯 emoji 不触发 LLM"},
    "trigger_msg_count": {"type": "int", "default": 10, "label": "整理触发消息数", "group": "整理",
                          "hint": "累计多少条消息后自动触发一次日记整理"},
    "trigger_time_minutes": {"type": "int", "default": 360, "label": "整理触发间隔(分钟)", "group": "整理",
                             "hint": "距上次整理超过此分钟数则触发（即使消息数未达阈值）"},
    "warmup_enabled": {"type": "bool", "default": True, "label": "暖启动", "group": "整理",
                       "hint": "前几次整理的阈值从低到高指数增长，避免初期过度整理"},
    "idle_timeout_minutes": {"type": "int", "default": 30, "label": "闲置超时(分钟)", "group": "整理",
                             "hint": "用户超过此时间无新消息，自动触发一次整理"},
    "max_l1_retries": {"type": "int", "default": 3, "label": "LLM 调用重试次数", "group": "整理",
                       "hint": "写日记/提取原子时 LLM 调用失败的重试次数"},
    "persona_update_interval": {"type": "int", "default": 10, "label": "画像更新间隔(日记数)", "group": "整理",
                                "hint": "每写 N 篇日记触发一次用户画像更新"},
    "max_diary_tokens": {"type": "int", "default": 500, "label": "日记 token 上限", "group": "记忆",
                         "hint": "LLM 写日记的最大 token 数，越长日记越详细但越贵"},
    "decay_rate": {"type": "float", "default": 0.99, "label": "日衰减率", "group": "衰减",
                   "hint": "每天 importance × 衰减率。0.99 = 约 69 天减半, 0.95 = 约 14 天减半"},
    "decay_enabled": {"type": "bool", "default": True, "label": "启用衰减", "group": "衰减",
                      "hint": "关闭后重要度永不下降，记忆只增不减"},
    "expired_atom_ttl_days": {"type": "int", "default": 60, "label": "过期原子保留天数", "group": "衰减",
                              "hint": "被标记为 forgotten/dormant 的原子超过此天数后永久删除"},
    "max_summary_chars": {"type": "int", "default": 200, "label": "归档摘要最大字数", "group": "归档",
                          "hint": "归档日记时摘要的最大字数"},
    "archive_enabled": {"type": "bool", "default": True, "label": "启用归档", "group": "归档",
                        "hint": "关闭后日记永不归档，永久保留在主数据库"},
    "archive_path": {"type": "string", "default": "./memory_archive", "label": "归档目录", "group": "归档",
                     "hint": "归档 Markdown 文件输出目录，相对于 data_dir"},
}


@router.get("/v1/config")
async def get_config(core: MemoryCore = Depends(get_core)):
    """获取当前配置（含元数据）"""
    groups = {}
    for key, meta in _CONFIG_META.items():
        group = meta["group"]
        if group not in groups:
            groups[group] = []
        # 扁平键 → 读取嵌套配置
        if key.startswith("archive_"):
            sub_key = key.replace("archive_", "")
            archive_cfg = core.config.get("archive", {})
            current = archive_cfg.get(sub_key, meta["default"])
        else:
            current = core.config.get(key, meta["default"])
        groups[group].append({
            "key": key,
            "value": current,
            **meta,
        })
    return {"ok": True, "groups": groups}


@router.put("/v1/config")
async def update_config(body: dict, request: Request, core: MemoryCore = Depends(get_core)):
    """更新配置项（自动保存到磁盘）"""
    import json

    valid_keys = set(_CONFIG_META.keys())
    for key, value in body.items():
        if key not in valid_keys:
            continue
        meta = _CONFIG_META[key]
        if meta["type"] == "int":
            value = int(value)
        elif meta["type"] == "float":
            value = float(value)
        elif meta["type"] == "bool":
            value = bool(value)

        # 扁平键 → 嵌套配置（例如 archive_enabled → archive.enabled）
        if key.startswith("archive_"):
            sub_key = key.replace("archive_", "")
            if "archive" not in core.config:
                core.config["archive"] = {}
            core.config["archive"][sub_key] = value
        else:
            core.config[key] = value
    core.reload_config(core.config)

    # 持久化到 JSON 文件
    data_dir = getattr(request.app.state, "_data_dir", None)
    if data_dir:
        path = Path(data_dir) / "memori_config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(core.config, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return {"ok": True, "updated": list(body.keys())}


# ═══════════════════════════════════════════════════════════
#  模型提供商管理
# ═══════════════════════════════════════════════════════════

@router.get("/v1/providers")
async def get_providers(core: MemoryCore = Depends(get_core)):
    """获取已配置的 LLM 提供商列表"""
    providers = core.config.get("_providers", [])
    return {"ok": True, "providers": providers,
            "selected_main": core.config.get("llm_provider_id", ""),
            "selected_judge": core.config.get("judge_provider_id", "")}


@router.put("/v1/providers")
async def save_providers(body: dict, request: Request, core: MemoryCore = Depends(get_core)):
    """保存 LLM 提供商配置"""
    import json
    providers = body.get("providers", [])
    core.config["_providers"] = providers

    # 持久化
    data_dir = getattr(request.app.state, "_data_dir", None)
    if data_dir:
        path = Path(data_dir) / "memori_config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(core.config, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "count": len(providers)}
