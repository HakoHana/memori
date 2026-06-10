"""Pydantic 请求/响应模型"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ═══════════════════════════════════════════════════════════
#  事件
# ═══════════════════════════════════════════════════════════

class EventRequest(BaseModel):
    """提交消息事件"""
    user_id: str = Field(..., description="用户唯一标识")
    text: str = Field(..., description="消息原文")
    sender_name: str = Field("", description="发送者昵称")
    session_id: str = Field("", description="会话 ID")
    system_prompt: str = Field("", description="系统提示词")


class EventResponse(BaseModel):
    """事件处理结果"""
    ok: bool = True
    modified_text: str | None = Field(None, description="注入记忆后的消息文本")
    injected_count: int = Field(0, description="注入的记忆条数")
    recalled_count: int = Field(0, description="召回的原子数")


# ═══════════════════════════════════════════════════════════
#  记忆（原子）
# ═══════════════════════════════════════════════════════════

class MemoryAtomOut(BaseModel):
    """对外暴露的记忆原子"""
    id: int
    content: str = Field(..., max_length=200)
    type: str = Field(alias="atom_type")
    importance: float = Field(..., ge=0, le=1)
    date: str | None = Field(None, alias="diary_date")


class MemorySearchResult(BaseModel):
    results: list[MemoryAtomOut]
    total: int


class MemoryUpdateRequest(BaseModel):
    content: str | None = None
    importance: float | None = Field(None, ge=0, le=1)
    status: str | None = None


# ═══════════════════════════════════════════════════════════
#  日记
# ═══════════════════════════════════════════════════════════

class DiaryOut(BaseModel):
    id: int
    user_id: str
    date: str
    content: str
    importance: float | None = None
    mood: str | None = None
    topics: list[str] = []


class DiaryListOut(BaseModel):
    items: list[DiaryOut]
    total: int
    page: int
    size: int


class DiaryUpdateRequest(BaseModel):
    content: str


# ═══════════════════════════════════════════════════════════
#  图谱
# ═══════════════════════════════════════════════════════════

class GraphNodeOut(BaseModel):
    id: str
    type: str
    label: str


class GraphEdgeOut(BaseModel):
    source: str
    target: str
    label: str


class GraphQueryOut(BaseModel):
    nodes: list[GraphNodeOut]
    edges: list[GraphEdgeOut]


class GraphQueryRequest(BaseModel):
    entity: str = Field(..., description="实体名称")


# ═══════════════════════════════════════════════════════════
#  用户
# ═══════════════════════════════════════════════════════════

class UserOut(BaseModel):
    uid: str
    name: str
    tier: str = "new"
    summary: str | None = None


class UserDetailOut(BaseModel):
    uid: str
    summary: str = ""
    full_markdown: str = ""
    tags: list[str] = []
    tier: str = "new"


# ═══════════════════════════════════════════════════════════
#  系统
# ═══════════════════════════════════════════════════════════

class StatsOut(BaseModel):
    users: int = 0
    diaries: int = 0
    atoms: int = 0
    facts: int = 0
    graph_nodes: int = 0
    graph_edges: int = 0


class ErrorOut(BaseModel):
    ok: bool = False
    error: str
