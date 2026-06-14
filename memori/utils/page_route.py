"""WebUI Dashboard Route — Quart HTTP 适配层

只做三件事：
1. 从 Quart request 提取参数
2. 调用 PageService 对应方法
3. 返回响应

不含业务逻辑。
"""

from __future__ import annotations

from quart import request

from .page_service import PageService


class PageRoute:
    """Quart HTTP 路由处理 — 薄适配层"""

    def __init__(self, service: PageService):
        self.service = service

    def register_routes(self, register):
        """注册所有 API 路由到 AstrBot/FastAPI/等框架

        Args:
            register: 可调用对象 (path, handler, methods, description) -> None
        """
        prefix = "/Memory/page"

        register(f"{prefix}/stats", self.get_stats, ["GET"], "Memory stats")
        register(f"{prefix}/graph/overview", self.get_graph_overview, ["GET"], "Graph overview")
        register(f"{prefix}/graph/query", self.query_graph, ["POST"], "Graph query")
        register(f"{prefix}/memories", self.list_memories, ["GET"], "List memories")
        register(f"{prefix}/memories/detail", self.get_memory_detail, ["GET"], "Memory detail")
        register(f"{prefix}/memories/update", self.update_memory, ["POST"], "Update memory")
        register(f"{prefix}/memories/delete", self.delete_memory, ["POST"], "Delete memory")
        register(f"{prefix}/memories/batch-delete", self.batch_delete_memories, ["POST"], "Batch delete")
        register(f"{prefix}/memories/update-status", self.update_diary_status, ["POST"], "Update status")
        register(f"{prefix}/memories/batch-update", self.update_diary_status, ["POST"], "Batch update")
        register(f"{prefix}/memories/timeline", self.get_timeline, ["GET"], "Timeline")
        register(f"{prefix}/memories/day", self.get_day_detail, ["GET"], "Day detail")
        register(f"{prefix}/diary", self.get_diary, ["GET"], "Get diary")
        register(f"{prefix}/diary/update", self.update_diary, ["POST"], "Update diary")
        register(f"{prefix}/persona", self.get_persona, ["GET"], "Get persona")
        register(f"{prefix}/persona/update", self.update_persona, ["POST"], "Update persona")
        register(f"{prefix}/users", self.list_users, ["GET"], "List users")
        register(f"{prefix}/users/detail", self.get_user_detail, ["GET"], "User detail")
        register(f"{prefix}/archive/list", self.list_archived, ["GET"], "List archived")
        register(f"{prefix}/archive/restore", self.restore_archived, ["POST"], "Restore archive")

    # ═══════════════════════════════════════════════════
    #  路由处理 — 只做 HTTP 参数提取和响应转发
    # ═══════════════════════════════════════════════════

    async def get_stats(self):
        return await self.service.get_stats()

    async def get_graph_overview(self):
        return await self.service.get_graph_overview()

    async def query_graph(self):
        body = await request.get_json() or {}
        entity = body.get("entity", "")
        return await self.service.query_graph(entity)

    async def list_memories(self):
        page = int(request.args.get("page", 1))
        size = int(request.args.get("size", 20))
        return await self.service.list_memories(page, size)

    async def get_memory_detail(self):
        eid = int(request.args.get("id", 0))
        if not eid:
            return self.service._error("id is required")
        return await self.service.get_memory_detail(eid)

    async def update_memory(self):
        body = await request.get_json() or {}
        entry_id = body.get("memory_id") or body.get("id") or 0
        updates = {}
        for f in ("content", "importance", "status"):
            if f in body:
                updates[f] = body[f]
        field = body.get("field", "")
        if field and "value" in body:
            field_map = {"content": "content", "importance": "importance", "status": "status"}
            db_field = field_map.get(field)
            if db_field:
                updates[db_field] = body["value"]
        return await self.service.update_memory(entry_id, updates)

    async def delete_memory(self):
        body = await request.get_json() or {}
        eid = body.get("memory_id") or body.get("id") or 0
        return await self.service.delete_memory(eid)

    async def batch_delete_memories(self):
        body = await request.get_json() or {}
        ids = body.get("ids", [])
        return await self.service.batch_delete_memories(ids)

    async def update_diary_status(self):
        body = await request.get_json() or {}
        ids = body.get("ids", [])
        status = body.get("status", "active")
        return await self.service.update_diary_status(ids, status)

    async def get_timeline(self):
        year = request.args.get("year", "")
        month = request.args.get("month", "")
        return await self.service.get_timeline(year, month)

    async def get_day_detail(self):
        date = request.args.get("date", "")
        return await self.service.get_day_detail(date)

    async def get_diary(self):
        eid = request.args.get("id", 0, type=int)
        date = request.args.get("date", "")
        return await self.service.get_diary(eid, date)

    async def update_diary(self):
        body = await request.get_json() or {}
        date = body.get("date", "")
        content = body.get("content", "")
        return await self.service.update_diary(date, content)

    async def get_persona(self):
        uid = request.args.get("uid", "")
        return await self.service.get_persona(uid)

    async def update_persona(self):
        body = await request.get_json() or {}
        uid = body.get("uid", "")
        summary = body.get("summary", "")
        full_md = body.get("full_markdown", "")
        tags = body.get("tags", [])
        return await self.service.update_persona(uid, summary, full_md, tags)

    async def list_users(self):
        return await self.service.list_users()

    async def get_user_detail(self):
        uid = request.args.get("uid", "")
        return await self.service.get_user_detail(uid)

    async def list_archived(self):
        page = int(request.args.get("page", 1))
        size = int(request.args.get("size", 20))
        return await self.service.list_archived(page, size)

    async def restore_archived(self):
        body = await request.get_json() or {}
        ids = body.get("ids", [])
        return await self.service.restore_archived(ids)
