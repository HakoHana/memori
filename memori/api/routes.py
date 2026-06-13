"""FastAPI 路由 — 所有 RESTful 端点"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from ..core.memory_core import MemoryCore
from ..utils.context_formatter import fmt_ts
from .deps import get_core, get_current_user, authorized_user, authorized_write
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

@router.get("/v1/memories")
async def list_memories(
    request: Request,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    q: str = Query("", description="搜索关键词"),
    uid: str = Query("", description="用户 ID"),
    core: MemoryCore = Depends(get_core),
):
    """记忆列表（分页）或搜索"""
    # 搜索模式
    if q and uid:
        atoms = await core.retriever.recall(uid, q, 5)
        results = [
            {"id": a.atom_id, "content": a.content, "type": a.atom_type.value,
             "importance": a.importance, "date": a.diary_date}
            for a in atoms
        ]
        return {"ok": True, "results": results, "total": len(results)}

    # 列表模式
    items, total = await core.diary_store.list_paginated(uid or None, page, size)
    return {"ok": True, "items": items, "total": total}


@router.get("/v1/memories/{memory_id}", response_model=dict)
async def get_memory_detail(memory_id: int, core: MemoryCore = Depends(get_core)):
    """获取单条记忆详情"""
    diary = await core.diary_store.get_by_id(memory_id)
    if not diary:
        raise HTTPException(404, "记忆不存在")

    diary["atoms"] = await core.atom_store.fetch(
        "SELECT a.id, a.content, a.atom_type, a.importance FROM memory_atoms a "
        "JOIN atoms_diary_links l ON a.id = l.atom_id "
        "WHERE l.diary_id=? AND a.status='active' ORDER BY l.importance DESC",
        (memory_id,),
    )
    diary["atoms"] = [
        {"id": a[0], "content": a[1], "type": a[2], "importance": a[3]}
        for a in diary["atoms"]
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
    await core.diary_store.execute(f"UPDATE diary_entries SET {sets} WHERE id=?", vals)
    return {"ok": True}


@router.delete("/v1/memories/{memory_id}")
async def delete_memory(memory_id: int, core: MemoryCore = Depends(get_core)):
    """删除记忆"""
    from ..utils.page_service import PageService
    svc = PageService(core)
    result = await svc.delete_memory(memory_id)
    return {"ok": result.get("ok", True), "cleaned_atoms": result.get("data", {}).get("cleaned_atoms", 0)}


@router.get("/v1/memories/timeline")
async def get_timeline(
    uid: str = Query(..., description="用户 ID"),
    year: str = Query("", description="年份过滤"),
    month: str = Query("", description="月份过滤"),
    core: MemoryCore = Depends(get_core),
):
    """按时间线浏览记忆日期"""
    dates = await core.diary_store.get_timeline_dates(uid, year, month)
    return {"ok": True, "dates": dates}


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
    items, total = await core.diary_store.list_paginated(uid or None, page, size)
    return {"ok": True, "items": items, "total": total, "page": page, "size": size}


@router.get("/v1/diaries/{date}")
async def get_diary(
    date: str,
    uid: str = Query(..., description="用户 ID"),
    core: MemoryCore = Depends(get_core),
):
    """获取指定日期日记"""
    diary = await core.diary_store.get_by_id(
        (await core.diary_store.fetchone(
            "SELECT id FROM diary_entries WHERE user_id=? AND date=? ORDER BY id DESC LIMIT 1",
            (uid, date),
        ) or [0])[0]
    )
    if not diary:
        raise HTTPException(404, "该日期没有日记")
    return {"ok": True, "data": diary}


@router.put("/v1/diaries/{date}")
async def update_diary(
    date: str,
    body: DiaryUpdateRequest,
    uid: str = Query(..., description="用户 ID"),
    core: MemoryCore = Depends(get_core),
):
    """更新指定日期日记"""
    from ..utils.diary_helper import parse_diary_content, mood_to_sentiment
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
    """图谱概览（新表 nodes/edges）"""
    stats = await core.graph_store.get_overview_stats()
    return {"ok": True, **stats}


@router.post("/v1/graph/query")
async def graph_query(body: GraphQueryRequest, core: MemoryCore = Depends(get_core)):
    """实体邻居查询（使用 core.graph_engine 单例）"""
    if not core.graph_engine:
        return {"ok": False, "error": "图谱引擎未初始化"}
    result = await core.graph_engine.query_neighbors(body.entity)
    return {
        "ok": True,
        "nodes": result.get("nodes", []),
        "edges": result.get("edges", []),
    }


# ═══════════════════════════════════════════════════════════
#  用户
# ═══════════════════════════════════════════════════════════

@router.get("/v1/users")
async def list_users(core: MemoryCore = Depends(get_core)):
    """用户列表"""
    users = await core.atom_store.list_users_with_persona()
    return {"ok": True, "users": users}


@router.get("/v1/users/{uid}")
async def get_user_detail(uid: str, core: MemoryCore = Depends(get_core)):
    """用户详情"""
    data = await core.atom_store.get_user_persona(uid)
    if not data:
        raise HTTPException(404, "用户不存在")
    return {"ok": True, "data": data}


@router.get("/v1/users/{uid}/persona")
async def get_persona(uid: str, core: MemoryCore = Depends(get_core)):
    """获取用户画像"""
    data = await core.atom_store.get_user_persona(uid)
    if not data:
        return {"ok": True, "summary": "", "full_markdown": "", "tags": []}
    tags = []
    try:
        raw = data.get("tags", "[]")
        tags = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        tags = []
    return {"ok": True, "summary": data.get("summary", ""),
            "full_markdown": data.get("full_markdown", ""), "tags": tags}


@router.get("/v1/graph/user/{display_name}/persona")
async def get_persona_from_graph(
    display_name: str,
    core: MemoryCore = Depends(get_core),
    user_id: str = Depends(get_current_user),
):
    """从图谱 user 节点（display_name）查到用户画像

    桥接图谱 → 画像：
      graph node "user:{display_name}"  →  user_identities → user_persona

    权限：仅当目标用户在当前用户的邻居列表中才返回完整数据
    """
    if not core.graph_engine:
        raise HTTPException(400, "图谱引擎未初始化")

    # 查看是否有社交关系（邻居检查）
    neighbors = await core.graph_engine.get_neighbor_ids(user_id)
    neighbor_weight = neighbors.get(display_name, 0)

    persona = await core.graph_engine.get_persona_from_graph_node(display_name)
    if not persona:
        raise HTTPException(404, "未找到该用户的画像")

    # 没社交关系：只返回基本信息
    if neighbor_weight <= 0:
        return {"ok": True, "data": {"name": persona["name"], "accessible": False}}

    # 有社交关系：根据 weight 返回不同粒度
    if neighbor_weight >= 0.6:
        return {"ok": True, "data": {**persona, "accessible": True}}
    elif neighbor_weight >= 0.4:
        return {"ok": True, "data": {
            "name": persona["name"],
            "summary": persona["summary"][:200],
            "tier": persona["tier"],
            "accessible": True,
        }}
    else:
        return {"ok": True, "data": {
            "name": persona["name"],
            "tier": persona["tier"],
            "accessible": True,
        }}


# ═══════════════════════════════════════════════════════════
#  系统
# ═══════════════════════════════════════════════════════════

@router.get("/v1/stats")
async def get_stats(core: MemoryCore = Depends(get_core)):
    """系统统计"""
    return {
        "ok": True,
        "users": (await core.atom_store.fetchone("SELECT COUNT(DISTINCT uid) FROM user_persona"))[0] or 0,
        "diaries": (await core.diary_store.fetchone("SELECT COUNT(*) FROM diary_entries"))[0] or 0,
        "atoms": (await core.atom_store.fetchone("SELECT COUNT(*) FROM memory_atoms WHERE status='active'"))[0] or 0,
        "graph_nodes": (await core.graph_store.fetchone("SELECT COUNT(*) FROM nodes"))[0] or 0,
        "graph_edges": (await core.graph_store.fetchone("SELECT COUNT(*) FROM edges WHERE status='active'"))[0] or 0,
    }


@router.post("/v1/archive/run")
async def trigger_archive(core: MemoryCore = Depends(get_core)):
    """手动触发归档"""
    if not hasattr(core, 'lifecycle') or not core.lifecycle or not core.lifecycle.archiver:
        raise HTTPException(400, "归档模块未启用")
    archived = await core.lifecycle.archiver.archive_daily()
    return {"ok": True, "archived": archived}


@router.post("/v1/decay/run")
async def trigger_decay(core: MemoryCore = Depends(get_core)):
    """手动触发重要度衰减"""
    if not hasattr(core, 'lifecycle') or not core.lifecycle:
        raise HTTPException(400, "生命周期管理器未启用")
    count = await core.lifecycle.decay.apply_global_decay()
    return {"ok": True, "decay_count": count}


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
    "embed_provider_id": {"type": "string", "default": "", "label": "嵌入模型", "group": "基础",
                          "hint": "用于向量检索的嵌入模型。需已在模型提供商中配置。留空 = 不启用向量检索"},
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
    "injection_max_tokens": {"type": "int", "default": 600, "label": "注入最大 token 数", "group": "注入",
                             "hint": "注入记忆的总 token 预算（含画像和日记片段）"},
    "injection_max_diaries": {"type": "int", "default": 2, "label": "溯源日记数量", "group": "注入",
                              "hint": "从最相关原子回溯的日记段落数，0=不展示日记"},
    "persona_mode": {"type": "select", "default": "tags", "options": ["tags", "summary", "full"],
                     "label": "画像模式", "group": "注入",
                     "hint": "tags=标签, summary=一句话摘要, full=完整描述"},
    "pre_filter_enabled": {"type": "bool", "default": False, "label": "预过滤（新用户降噪）", "group": "注入",
                           "hint": "启用后新用户短消息/重复消息/纯 emoji 不触发 LLM"},
    "hotcache_max_per_user": {"type": "int", "default": 50, "label": "热缓存容量(条)", "group": "整理",
                               "hint": "每用户热缓存最多保留多少条消息，超出的最早消息被丢弃"},
    "consolidation_rounds": {"type": "int", "default": 5, "label": "整理触发轮数", "group": "整理",
                              "hint": "Bot 参与对话达到此轮数后自动触发日记整理"},
    "idle_timeout_minutes": {"type": "int", "default": 60, "label": "空闲超时(分钟)", "group": "整理",
                              "hint": "用户超过此时间无新消息，自动整理未处理的消息"},
    "min_global_interval": {"type": "int", "default": 120, "label": "全局限速(秒)", "group": "整理",
                             "hint": "两次全局整理最短间隔，防大量用户同时触发"},
    "min_user_interval": {"type": "int", "default": 60, "label": "用户限速(秒)", "group": "整理",
                           "hint": "同一用户两次整理最短间隔，防刷屏重复触发"},
    "scan_interval_minutes": {"type": "int", "default": 120, "label": "定时扫描(分钟)", "group": "整理",
                               "hint": "不管用户是否活跃，距上次整理超过此时间则扫描积压内容"},
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
    "agent_recall_tool_enabled": {"type": "bool", "default": True, "label": "启用主动回忆工具", "group": "Agent",
                                   "hint": "开启后注册 recall_long_term_memory，允许 Agent 主动检索长期记忆"},
    "agent_memorize_tool_enabled": {"type": "bool", "default": False, "label": "启用主动记忆写入工具", "group": "Agent",
                                     "hint": "开启后注册 memorize_long_term_memory，允许 Agent 主动写入长期记忆"},
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


@router.post("/v1/shutdown")
async def shutdown(user_id: str = Depends(get_current_user)):
    """停止 memori 服务（需管理员权限）"""
    # TODO: 接入管理员白名单校验
    import os
    import logging
    logging.getLogger("memori").warning(f"[memori] 管理员 {user_id} 请求关闭服务")
    os._exit(0)


# ═══════════════════════════════════════════════════════════
#  社交关系管理
# ═══════════════════════════════════════════════════════════


class ClaimRequest(BaseModel):
    target_uid: str
    relation_type: str = "friend_of"


@router.post("/v1/relations/claim")
async def claim_relation(
    body: ClaimRequest,
    core: MemoryCore = Depends(get_core),
    user_id: str = Depends(get_current_user),
):
    """A 声称与 B 的关系"""
    if not core.graph_engine:
        raise HTTPException(400, "图谱引擎未初始化")
    if body.target_uid == user_id:
        raise HTTPException(400, "不能与自己建立关系")
    result = await core.graph_engine.claim_relationship(
        user_id, body.target_uid, body.relation_type
    )
    if not result:
        raise HTTPException(409, "声称关系失败（可能已被对方屏蔽）")
    return {"ok": True, "edge_id": result.edge_id, "status": result.status}


@router.post("/v1/relations/{edge_id}/confirm")
async def confirm_relation(
    edge_id: str,
    core: MemoryCore = Depends(get_core),
    user_id: str = Depends(get_current_user),
):
    """确认关系声称（edge_id = social:claimer:type:target）"""
    if not core.graph_engine:
        raise HTTPException(400, "图谱引擎未初始化")
    parts = edge_id.split(":", 2)
    if len(parts) < 3:
        raise HTTPException(400, "无效的 edge_id")
    claimer_uid = parts[1]
    result = await core.graph_engine.confirm_relationship(user_id, claimer_uid)
    if not result:
        raise HTTPException(404, "待确认的关系不存在")
    return {"ok": True, "edge_id": result.edge_id, "status": result.status}


@router.post("/v1/relations/{edge_id}/reject")
async def reject_relation(
    edge_id: str,
    core: MemoryCore = Depends(get_core),
    user_id: str = Depends(get_current_user),
):
    """拒绝关系声称"""
    if not core.graph_engine:
        raise HTTPException(400, "图谱引擎未初始化")
    parts = edge_id.split(":", 2)
    if len(parts) < 3:
        raise HTTPException(400, "无效的 edge_id")
    claimer_uid = parts[1]
    ok = await core.graph_engine.reject_relationship(user_id, claimer_uid)
    if not ok:
        raise HTTPException(404, "待确认的关系不存在")
    return {"ok": True}


@router.post("/v1/relations/block")
async def block_user(
    body: ClaimRequest,
    core: MemoryCore = Depends(get_core),
    user_id: str = Depends(get_current_user),
):
    """屏蔽用户"""
    if not core.graph_engine:
        raise HTTPException(400, "图谱引擎未初始化")
    ok = await core.graph_engine.block_user(user_id, body.target_uid)
    if not ok:
        raise HTTPException(500, "屏蔽失败")
    from . import _get_auth_manager
    _get_auth_manager().invalidate_cache(user_id)
    return {"ok": True}


@router.get("/v1/relations/neighbors")
async def get_neighbors(
    min_weight: float = Query(0.0, ge=0.0, le=1.0),
    core: MemoryCore = Depends(get_core),
    user_id: str = Depends(get_current_user),
):
    """获取我的社交邻居列表"""
    if not core.graph_engine:
        return {"ok": True, "neighbors": {}}
    neighbors = await core.graph_engine.get_neighbor_ids(user_id)
    filtered = {uid: w for uid, w in neighbors.items() if uid != user_id and w >= min_weight}
    return {"ok": True, "neighbors": filtered}


@router.get("/v1/relations/pending")
async def get_pending_relations(
    core: MemoryCore = Depends(get_core),
    user_id: str = Depends(get_current_user),
):
    """获取待确认的关系列表"""
    if not core.graph_engine:
        return {"ok": True, "pending": []}
    edges = await core.graph_store.query_pending_confirmations(user_id)
    pending = []
    for e in edges:
        pending.append({
            "edge_id": e.edge_id,
            "from_user": e.from_user,
            "relation_type": e.relation_type,
            "weight": e.weight,
            "created_at": fmt_ts(e.created_at),
        })
    return {"ok": True, "pending": pending}


@router.delete("/v1/relations/{edge_id}")
async def remove_relation(
    edge_id: str,
    core: MemoryCore = Depends(get_core),
    user_id: str = Depends(get_current_user),
):
    """解除关系"""
    try:
        parts = edge_id.split(":", 3)
        uid_a, uid_b = parts[1], parts[3]
        if user_id not in (uid_a, uid_b):
            raise HTTPException(403, "无权操作此关系")
    except (IndexError, ValueError):
        raise HTTPException(400, "无效的 edge_id")
    await core.graph_store.set_social_edge_status(edge_id, "rejected")
    from . import _get_auth_manager
    _get_auth_manager().invalidate_cache(user_id)
    return {"ok": True}


# ═══════════════════════════════════════════════════════════
#  API Key 管理
# ═══════════════════════════════════════════════════════════


@router.post("/v1/api-keys")
async def create_api_key(
    user_id: str = Depends(get_current_user),
):
    """生成新的 API Key"""
    from . import _get_auth_manager
    key = _get_auth_manager().generate_api_key(user_id)
    return {"ok": True, "api_key": key}


@router.get("/v1/api-keys")
async def list_my_api_keys(
    user_id: str = Depends(get_current_user),
):
    """列出我的 API Keys"""
    from . import _get_auth_manager
    keys = _get_auth_manager().list_api_keys()
    my_keys = [k for k in keys if k["user_id"] == user_id]
    return {"ok": True, "keys": my_keys}


@router.delete("/v1/api-keys/{key_id}")
async def revoke_api_key(
    key_id: str,
    user_id: str = Depends(get_current_user),
):
    """撤销 API Key"""
    from . import _get_auth_manager
    if not _get_auth_manager().revoke_api_key(key_id):
        raise HTTPException(404, "API Key 不存在")
    return {"ok": True}


@router.post("/v1/tools/read_diary", response_model=ReadDiaryResponse)
async def read_diary(body: ReadDiaryRequest, core: MemoryCore = Depends(get_core)):
    """读取完整日记（供 Agent 工具使用）"""
    row = await core.diary_store.fetchone(
        "SELECT id, user_id, date, content, importance FROM diary_entries WHERE id=?",
        (body.diary_id,),
    )
    if not row:
        raise HTTPException(404, "日记不存在")

    # 关联原子
    atoms = await core.atom_store.fetch(
        "SELECT a.id, a.content, a.atom_type, a.importance FROM memory_atoms a "
        "JOIN atoms_diary_links l ON a.id = l.atom_id "
        "WHERE l.diary_id=? AND a.status='active' ORDER BY l.importance DESC",
        (body.diary_id,),
    )

    return ReadDiaryResponse(
        diary_id=row[0],
        date=row[2] or "",
        content=row[3] or "",
        importance=row[4] or 0.0,
        atoms=[
            {"id": a[0], "content": a[1], "type": a[2], "importance": a[3]}
            for a in atoms
        ],
    )
