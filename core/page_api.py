"""WebUI Dashboard API — 注册 AstrBot 原生页面 API"""

from __future__ import annotations

import json
import time
from typing import Any, TYPE_CHECKING

from astrbot.api import logger

if TYPE_CHECKING:
    from ..core.memory_core import MemoryCore


class PageApi:
    """Memory 插件 Dashboard API"""

    def __init__(self, memory_core: MemoryCore):
        self.core = memory_core

    def register_routes(self, context):
        """注册所有 API 路由"""
        register = context.register_web_api
        prefix = "/Memory/page"

        register(f"{prefix}/stats", self.get_stats, ["GET"], "Memory stats")
        register(f"{prefix}/graph/overview", self.get_graph_overview, ["GET"], "Graph overview")
        register(f"{prefix}/graph/query", self.query_graph, ["POST"], "Graph query")
        register(f"{prefix}/memories", self.list_memories, ["GET"], "List memories")
        register(f"{prefix}/memories/detail", self.get_memory_detail, ["GET"], "Memory detail")
        register(f"{prefix}/memories/update", self.update_memory, ["POST"], "Update memory")
        register(f"{prefix}/memories/delete", self.delete_memory, ["POST"], "Delete memory")
        register(f"{prefix}/memories/timeline", self.get_timeline, ["GET"], "Memory timeline")
        register(f"{prefix}/memories/day", self.get_day_detail, ["GET"], "Memory day detail")
        register(f"{prefix}/diary", self.get_diary, ["GET"], "Get diary content")
        register(f"{prefix}/diary/update", self.update_diary, ["POST"], "Update diary")
        register(f"{prefix}/persona", self.get_persona, ["GET"], "Get persona")
        register(f"{prefix}/persona/update", self.update_persona, ["POST"], "Update persona")
        register(f"{prefix}/import/livingmemory", self.import_livingmemory, ["POST"], "Import from livingmemory")
        register(f"{prefix}/providers", self.list_providers, ["GET"], "List LLM providers")

    async def list_providers(self):
        """列出 AstrBot 中已配置的 LLM Provider"""
        try:
            context = self.core.plugin_context
            provider_manager = getattr(context, "provider_manager", None)
            if not provider_manager:
                return self._ok({"providers": [], "current": ""})

            providers = []
            current_id = ""
            if hasattr(provider_manager, "curr_provider_inst") and provider_manager.curr_provider_inst:
                current = provider_manager.curr_provider_inst
                current_id = getattr(current, "provider_id", "") or getattr(current, "name", "")

            if hasattr(provider_manager, "provider_insts"):
                for p in provider_manager.provider_insts:
                    pid = getattr(p, "provider_id", "") or getattr(p, "name", "")
                    name = getattr(p, "provider_name", "") or pid
                    if pid:
                        providers.append({"id": pid, "name": name})

            return self._ok({"providers": providers, "current": current_id})
        except Exception as e:
            return self._ok({"providers": [], "current": "", "error": str(e)})

    async def get_stats(self):
        """概览统计"""
        try:
            atom_store = self.core.atom_store
            diary_store = self.core.diary_store
            graph_store = self.core.graph_store

            atom_stats = await atom_store.get_stats("Hana")
            diary_dates = await diary_store.list_months("Hana")
            graph_overview = await graph_store.get_graph_overview() if graph_store else {}

            return self._ok({
                "atoms": atom_stats,
                "diary_months": len(diary_dates),
                "graph_nodes": graph_overview.get("total_nodes", 0),
                "graph_edges": graph_overview.get("total_edges", 0),
            })
        except Exception as e:
            return self._error(str(e))

    async def get_graph_overview(self):
        """图谱概览"""
        try:
            data = await self.core.graph_store.get_full_graph(500)
            return self._ok(data)
        except Exception as e:
            return self._error(str(e))

    async def query_graph(self):
        """搜索图谱"""
        try:
            from quart import request
            body = await request.get_json()
            query = (body or {}).get("query", "")
            data = await self.core.graph_store.query_graph(query, 100)
            return self._ok(data)
        except Exception as e:
            return self._error(str(e))

    async def list_memories(self):
        """列出日记条目（非原子），按日期降序"""
        try:
            from quart import request
            q = request.args
            keyword = q.get("keyword", "").strip()
            user_id = q.get("user_id", "Hana")
            page = max(1, int(q.get("page", 1)))
            page_size = min(200, max(1, int(q.get("page_size", 50))))

            import sqlite3
            db = self.core.atom_store.db_path
            conn = sqlite3.connect(db)

            if keyword:
                rows = conn.execute("""
                    SELECT d.date, d.content, d.created_at,
                           (SELECT COUNT(*) FROM memory_atoms a WHERE a.diary_date=d.date AND a.status='active') as atom_count
                    FROM diary_entries d WHERE d.user_id=? AND d.content LIKE ?
                    ORDER BY d.date DESC LIMIT ? OFFSET ?
                """, (user_id, f"%{keyword}%", page_size, (page-1)*page_size)).fetchall()
                total = conn.execute(
                    "SELECT COUNT(*) FROM diary_entries WHERE user_id=? AND content LIKE ?",
                    (user_id, f"%{keyword}%")
                ).fetchone()[0]
            else:
                rows = conn.execute("""
                    SELECT d.date, d.content, d.created_at,
                           (SELECT COUNT(*) FROM memory_atoms a WHERE a.diary_date=d.date AND a.status='active') as atom_count
                    FROM diary_entries d WHERE d.user_id=?
                    ORDER BY d.date DESC LIMIT ? OFFSET ?
                """, (user_id, page_size, (page-1)*page_size)).fetchall()
                total = conn.execute(
                    "SELECT COUNT(*) FROM diary_entries WHERE user_id=?", (user_id,)
                ).fetchone()[0]

            conn.close()

            items = []
            for r in rows:
                date_str, content, created_ts, atom_cnt = r
                preview = content.strip()
                if "## " in preview:
                    preview = preview.split("## ")[-1]
                if len(preview) > 200:
                    preview = preview[:200] + "..."
                items.append({
                    "date": date_str,
                    "content": preview,
                    "created_at": created_ts,
                    "atom_count": atom_cnt,
                })

            return self._ok({
                "total": total,
                "page": page,
                "page_size": page_size,
                "items": items,
            })
        except Exception as e:
            return self._error(str(e))

    async def get_memory_detail(self):
        """单条记忆详情"""
        try:
            from quart import request
            atom_id = int(request.args.get("id", 0))
            atom = await self.core.atom_store.get_by_id(atom_id)
            if not atom:
                return self._error("未找到")
            return self._ok(self._atom_dict(atom))
        except Exception as e:
            return self._error(str(e))

    async def update_memory(self):
        """更新记忆"""
        try:
            from quart import request
            body = await request.get_json()
            atom_id = body.get("id", 0)
            updates = {}
            for field in ("content", "atom_type", "importance"):
                if field in body:
                    updates[field] = body[field]
            ok = await self.core.atom_store.update_atom(atom_id, **updates)
            return self._ok({"updated": ok})
        except Exception as e:
            return self._error(str(e))

    async def delete_memory(self):
        """软删除记忆"""
        try:
            from quart import request
            body = await request.get_json()
            atom_id = body.get("id", 0)
            user_id = body.get("user_id", "Hana")
            ok = await self.core.atom_store.delete(atom_id, user_id)
            return self._ok({"deleted": ok})
        except Exception as e:
            return self._error(str(e))

    async def get_timeline(self):
        """按时间线列出所有记忆"""
        try:
            from quart import request
            q = request.args
            user_id = q.get("user_id", "Hana")
            page = max(1, int(q.get("page", 1)))
            page_size = min(100, max(1, int(q.get("page_size", 20))))
            data = await self.core.atom_store.get_timeline(user_id, page, page_size)
            # 补充日记预览
            for item in data["items"]:
                diary = await self.core.diary_store.read(user_id, item["date"])
                item["diary_preview"] = (diary[:200] if diary else "") if diary else ""
                item["has_diary"] = diary is not None
            return self._ok(data)
        except Exception as e:
            return self._error(str(e))

    async def get_day_detail(self):
        """获取单天的完整记忆详情"""
        try:
            from quart import request
            q = request.args
            user_id = q.get("user_id", "Hana")
            date = q.get("date", "")

            # 日记 — 通过 DiaryStore
            diary_content = await self.core.diary_store.read(user_id, date)

            # 原子 — 通过 AtomStore
            atoms = await self.core.atom_store.get_day_atoms(user_id, date)

            # 图谱
            graph = self.core.graph_store
            gdata = await graph.query_graph(date, 30) if graph else {"nodes": [], "edges": []}

            return self._ok({
                "date": date,
                "diary": {"content": diary_content or ""} if diary_content else None,
                "atoms": atoms,
                "graph": gdata,
            })
        except Exception as e:
            return self._error(str(e))

    async def get_diary(self):
        """获取日记内容"""
        try:
            from quart import request
            user_id = request.args.get("user_id", "Hana")
            date = request.args.get("date", "")
            content = await self.core.diary_store.read(user_id, date)
            return self._ok({"date": date, "content": content or ""})
        except Exception as e:
            return self._error(str(e))

    async def update_diary(self):
        """更新日记"""
        try:
            from quart import request
            body = await request.get_json()
            user_id = body.get("user_id", "Hana")
            date = body.get("date", "")
            content = body.get("content", "")
            await self.core.diary_store.upsert(user_id, date, content)
            return self._ok({"saved": True})
        except Exception as e:
            return self._error(str(e))

    async def get_persona(self):
        """获取画像"""
        try:
            from quart import request
            user_id = request.args.get("user_id", "Hana")
            persona = await self.core.persona_store.read(user_id)
            return self._ok({"persona": persona or ""})
        except Exception as e:
            return self._error(str(e))

    async def update_persona(self):
        """更新画像"""
        try:
            from quart import request
            body = await request.get_json()
            user_id = body.get("user_id", "Hana")
            content = body.get("content", "")
            await self.core.persona_store.write(user_id, content)
            return self._ok({"saved": True})
        except Exception as e:
            return self._error(str(e))

    async def import_livingmemory(self):
        """从 livingmemory 导入"""
        try:
            from quart import request
            body = await request.get_json()
            source = body.get("source", "/home/hako/data/plugin_data/astrbot_plugin_livingmemory/livingmemory.db")
            import subprocess, sys
            script = str(self.core.data_dir.parent / "scripts" / "import_from_livingmemory.py")
            result = subprocess.run(
                [sys.executable, script,
                 "--source", source,
                 "--target", self.core.atom_store.db_path,
                 "--data-dir", str(self.core.data_dir),
                 "--default-user", "Hana"],
                capture_output=True, text=True, timeout=120,
            )
            return self._ok({"stdout": result.stdout, "stderr": result.stderr, "returncode": result.returncode})
        except Exception as e:
            return self._error(str(e))

    def _atom_dict(self, atom) -> dict:
        """MemoryAtom -> dict for API"""
        return {
            "id": atom.atom_id,
            "user_id": atom.user_id,
            "date": atom.diary_date,
            "type": atom.atom_type.value,
            "content": atom.content,
            "importance": round(atom.importance, 2),
            "confidence": round(atom.confidence, 2),
            "access_count": atom.access_count,
            "entities": atom.entities,
            "diary_snippet": atom.diary_snippet,
            "expires_at": atom.expires_at,
            "decay_type": atom.decay_type.value if hasattr(atom.decay_type, 'value') else str(atom.decay_type),
            "created_at": atom.created_at,
        }

    @staticmethod
    def _ok(data: Any) -> dict:
        return {"status": "ok", "data": data}

    @staticmethod
    def _error(msg: str) -> dict:
        return {"status": "error", "message": str(msg)}
