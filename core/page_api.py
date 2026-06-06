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
        register(f"{prefix}/memories/batch-delete", self.batch_delete_memories, ["POST"], "Batch delete memories")
        register(f"{prefix}/memories/update-status", self.update_diary_status, ["POST"], "Update diary status")
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
        try:
            from quart import request
            q = request.args
            keyword = q.get("keyword", "").strip()
            user_id = q.get("user_id", "Hana")
            year = q.get("year", "")
            month = q.get("month", "")
            page = max(1, int(q.get("page", 1)))
            page_size = min(200, max(1, int(q.get("page_size", 50))))

            import sqlite3
            db = self.core.atom_store.db_path
            conn = sqlite3.connect(db)

            conditions = ["d.user_id = ?"]
            params = [user_id]
            if year:
                conditions.append("substr(d.date,1,4) = ?")
                params.append(year)
            if month:
                conditions.append("substr(d.date,6,2) = ?")
                params.append(month.zfill(2))
            if keyword:
                if keyword.isdigit():
                    conditions.append("(d.id = ? OR d.content LIKE ?)")
                    params.extend([int(keyword), f"%{keyword}%"])
                else:
                    conditions.append("d.content LIKE ?")
                    params.append(f"%{keyword}%")

            where = " AND ".join(conditions)
            cursor = conn.execute(f"""
                SELECT d.id, d.date, d.content, d.created_at, d.updated_at, COALESCE(d.status,'active'),
                       (SELECT COUNT(*) FROM memory_atoms a WHERE a.diary_id=d.id AND a.status='active'),
                       (SELECT ROUND(AVG(a.importance),2) FROM memory_atoms a WHERE a.diary_id=d.id AND a.status='active')
                FROM diary_entries d WHERE {where}
                ORDER BY d.id DESC LIMIT ? OFFSET ?
            """, params + [page_size, (page-1)*page_size])
            rows = cursor.fetchall()
            total = conn.execute(f"SELECT COUNT(*) FROM diary_entries d WHERE {where}", params).fetchone()[0]
            conn.close()

            items = []
            for r in rows:
                did, dt, content, cts, uts, st, acnt, aimp = r
                preview = (content or "")[:150]
                if len(content or "") > 150:
                    preview += "..."
                items.append({
                    "id": did, "date": dt, "content": preview,
                    "created_at": cts, "updated_at": uts or cts,
                    "status": st, "atom_count": acnt, "avg_importance": aimp,
                    "types": self._get_atom_types_for_date(dt, did),
                })
            return self._ok({"total": total, "page": page, "page_size": page_size, "items": items})
        except Exception as e:
            return self._error(str(e))

    def _get_atom_types_for_date(self, date_str: str, diary_id: int = 0) -> list:
        try:
            import sqlite3
            conn = sqlite3.connect(self.core.atom_store.db_path)
            if diary_id:
                rows = conn.execute("SELECT atom_type, COUNT(*) FROM memory_atoms WHERE diary_id=? AND status='active' GROUP BY atom_type ORDER BY COUNT(*) DESC", (diary_id,)).fetchall()
            else:
                rows = conn.execute("SELECT atom_type, COUNT(*) FROM memory_atoms WHERE diary_date=? AND status='active' GROUP BY atom_type ORDER BY COUNT(*) DESC", (date_str,)).fetchall()
            conn.close()
            return [{"type": r[0], "count": r[1]} for r in rows]
        except Exception:
            return []

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
        """删除整篇日记及其关键事实"""
        try:
            from quart import request
            body = await request.get_json()
            diary_id = body.get("id", 0)
            import sqlite3
            db = self.core.atom_store.db_path
            conn = sqlite3.connect(db)
            row = conn.execute("SELECT date FROM diary_entries WHERE id=?", (diary_id,)).fetchone()
            if row:
                conn.execute("DELETE FROM diary_entries WHERE id=?", (diary_id,))
                conn.execute("DELETE FROM memory_atoms WHERE diary_date=? AND user_id=?", (row[0], "Hana"))
            conn.commit()
            conn.close()
            return self._ok({"deleted": True})
        except Exception as e:
            return self._error(str(e))

    async def batch_delete_memories(self):
        try:
            from quart import request
            body = await request.get_json()
            ids = body.get("ids", [])
            import sqlite3
            db = self.core.atom_store.db_path
            conn = sqlite3.connect(db)
            for did in ids:
                row = conn.execute("SELECT date FROM diary_entries WHERE id=?", (did,)).fetchone()
                if row:
                    conn.execute("DELETE FROM diary_entries WHERE id=?", (did,))
                    conn.execute("DELETE FROM memory_atoms WHERE diary_date=? AND user_id=?", (row[0], "Hana"))
            conn.commit()
            conn.close()
            return self._ok({"deleted": len(ids)})
        except Exception as e:
            return self._error(str(e))

    async def update_diary_status(self):
        try:
            from quart import request
            body = await request.get_json()
            diary_id = body.get("id", 0)
            status = body.get("status", "active")
            import sqlite3, time
            db = self.core.atom_store.db_path
            conn = sqlite3.connect(db)
            conn.execute("UPDATE diary_entries SET status=?, updated_at=? WHERE id=?", (status, time.time(), diary_id))
            conn.commit()
            conn.close()
            return self._ok({"updated": True})
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
        """获取某篇日记及其关联的关键事实"""
        try:
            from quart import request
            q = request.args
            did = int(q.get("did", 0))

            import sqlite3
            db = self.core.atom_store.db_path
            conn = sqlite3.connect(db)

            # 读日记
            diary = conn.execute("SELECT id, date, content, topics, sentiment, status FROM diary_entries WHERE id=?", (did,)).fetchone()
            if not diary:
                conn.close()
                return self._error("未找到该日记")

            did_val, date_str, content, topics, sentiment, status = diary
            status = status or "active"

            # 读该日记关联的原子
            atom_rows = conn.execute("""
                SELECT id, content, atom_type, importance, diary_snippet
                FROM memory_atoms WHERE diary_id=? AND status='active'
                ORDER BY importance DESC
            """, (did,)).fetchall()

            atoms = []
            for r in atom_rows:
                atoms.append({
                    "id": r[0], "content": r[1], "type": r[2],
                    "importance": r[3], "snippet": r[4] or "",
                })

            # 重要度统计
            imp_stats = {"avg": 0, "max": 0, "count": len(atoms)}
            if atoms:
                imps = [a["importance"] for a in atoms]
                imp_stats["avg"] = round(sum(imps)/len(imps), 2)
                imp_stats["max"] = max(imps)

            conn.close()

            return self._ok({
                "date": date_str,
                "status": status,
                "diary": {"content": content or "", "topics": topics or "", "sentiment": sentiment or ""},
                "imp_stats": imp_stats,
                "atoms": atoms,
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
