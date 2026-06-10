"""WebUI Dashboard API — 无框架依赖，通过回调注册路由"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, TYPE_CHECKING

from .logger import logger

if TYPE_CHECKING:
    from ..core.memory_core import MemoryCore


class PageApi:
    """Memory 插件 Dashboard API

    所有数据库操作通过共享连接池执行。
    路由注册通过外部回调实现，不依赖特定 Web 框架。
    """

    def __init__(self, memory_core: MemoryCore):
        self.core = memory_core

    def register_routes(self, register: Callable):
        """注册所有 API 路由

        Args:
            register: 一个可调用对象，签名 (path, handler, methods, description) -> None
                      由上层框架（AstrBot / FastAPI / Quart 等）实现
        """
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
        register(f"{prefix}/memories/batch-update", self.update_diary_status, ["POST"], "Batch update diary (alias)")
        register(f"{prefix}/memories/timeline", self.get_timeline, ["GET"], "Memory timeline")
        register(f"{prefix}/memories/day", self.get_day_detail, ["GET"], "Memory day detail")
        register(f"{prefix}/diary", self.get_diary, ["GET"], "Get diary content")
        register(f"{prefix}/diary/update", self.update_diary, ["POST"], "Update diary")
        register(f"{prefix}/persona", self.get_persona, ["GET"], "Get persona")
        register(f"{prefix}/persona/update", self.update_persona, ["POST"], "Update persona")
        register(f"{prefix}/users", self.list_users, ["GET"], "List users")
        register(f"{prefix}/users/detail", self.get_user_detail, ["GET"], "User detail with memories")
        register(f"{prefix}/archive/list", self.list_archived, ["GET"], "List archived entries")
        register(f"{prefix}/archive/restore", self.restore_archived, ["POST"], "Restore from archive")

    # ── 复用 Store 的异步连接池 ──

    @property
    def _db(self):
        return self.core.atom_store

    async def _fetch(self, sql: str, params: tuple | list | None = None) -> list:
        return await self._db.fetch(sql, params)

    async def _fetchone(self, sql: str, params: tuple | list | None = None):
        return await self._db.fetchone(sql, params)

    async def _execute(self, sql: str, params: tuple | list | None = None):
        return await self._db.execute(sql, params)

    # ── 辅助 ──

    @staticmethod
    def _ok(data: Any) -> dict:
        return {"ok": True, "data": data}

    @staticmethod
    def _error(msg: str) -> dict:
        return {"ok": False, "error": msg}

    @staticmethod
    def _parse_date(d: str) -> str:
        """标准化日期格式"""
        d = d.replace("/", "-")
        parts = d.split("-")
        if len(parts) == 3:
            return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
        return d

    # ═══════════════════════════════════════════════════
    #  API 处理
    # ═══════════════════════════════════════════════════

    async def get_stats(self):
        """概览统计"""
        try:
            uid = "Hana"
            user_count = (await self._fetchone("SELECT COUNT(DISTINCT uid) FROM user_persona"))[0] or 0
            diary_count = (await self._fetchone("SELECT COUNT(*) FROM diary_entries"))[0] or 0
            atom_count = (await self._fetchone("SELECT COUNT(*) FROM memory_atoms WHERE status='active'"))[0] or 0
            fact_count = (await self._fetchone("SELECT COUNT(*) FROM atomic_facts"))[0] or 0
            node_count = (await self._fetchone("SELECT COUNT(*) FROM graph_nodes"))[0] or 0
            edge_count = (await self._fetchone("SELECT COUNT(*) FROM graph_edges"))[0] or 0
            return self._ok({
                "users": user_count,
                "diaries": diary_count,
                "atoms": atom_count,
                "facts": fact_count,
                "graph_nodes": node_count,
                "graph_edges": edge_count,
            })
        except Exception as e:
            return self._error(str(e))

    async def get_graph_overview(self):
        """图谱概览：节点类型分布"""
        try:
            rows = await self._fetch("SELECT node_type, COUNT(*) FROM graph_nodes GROUP BY node_type")
            nodes = {r[0]: r[1] for r in rows}
            relation_rows = await self._fetch("SELECT relation_type, COUNT(*) FROM graph_edges GROUP BY relation_type")
            edges = {r[0]: r[1] for r in relation_rows}
            return self._ok({"nodes": nodes, "edges": edges})
        except Exception as e:
            return self._error(str(e))

    async def query_graph(self):
        """图查询：实体邻居查询"""
        try:
            from ..core.graph_engine import GraphEngine
            body = await self._get_json()
            entity = (body or {}).get("entity", "")
            if not entity:
                return self._ok({"nodes": [], "edges": []})
            ge = GraphEngine(
                graph_store=self.core.graph_store,
                atom_store=self.core.atom_store,
                diary_store=self.core.diary_store,
            )
            result = await ge.query_neighbors(entity)
            return self._ok({
                "nodes": [{"id": n.node_key, "type": n.node_type, "label": n.value} for n in result.get("nodes", [])],
                "edges": [{"from": e.source, "to": e.target, "label": e.relation_type} for e in result.get("edges", [])],
            })
        except Exception as e:
            return self._error(str(e))

    async def list_memories(self):
        """日记列表（分页）"""
        try:
            from quart import request
            uid = request.args.get("uid", "") or request.args.get("user_id", "")
            page = int(request.args.get("page", 1))
            size = int(request.args.get("size", 20))
            offset = (page - 1) * size

            if uid:
                rows = await self._fetch(
                    "SELECT id, user_id, date, importance, sentiment, topics, created_at FROM diary_entries WHERE user_id=? ORDER BY date DESC LIMIT ? OFFSET ?",
                    (uid, size, offset),
                )
                total = (await self._fetchone("SELECT COUNT(*) FROM diary_entries WHERE user_id=?", (uid,)))[0]
            else:
                rows = await self._fetch(
                    "SELECT id, user_id, date, importance, sentiment, topics, created_at FROM diary_entries ORDER BY date DESC LIMIT ? OFFSET ?",
                    (size, offset),
                )
                total = (await self._fetchone("SELECT COUNT(*) FROM diary_entries"))[0]

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
                    "created_at": r[6],
                })
            return self._ok({"items": items, "total": total, "page": page, "size": size})
        except Exception as e:
            return self._error(str(e))

    async def get_memory_detail(self):
        """日记详情"""
        try:
            from quart import request
            eid = int(request.args.get("id", 0))
            if not eid:
                return self._error("id is required")
            row = await self._fetchone("SELECT * FROM diary_entries WHERE id=?", (eid,))
            if not row:
                return self._error("not found")

            columns = ["id", "uid", "user_id", "date", "timestamp", "content", "importance", "mood", "topics", "sentiment",
                       "fact_extracted", "fact_retry_count", "archived", "correction", "created_at"]
            diary = dict(zip(columns, row))
            diary["id"] = diary["id"]

            # 关联原子
            atoms = await self._fetch(
                "SELECT id, content, atom_type, importance FROM memory_atoms WHERE diary_id=? AND status='active' ORDER BY importance DESC",
                (eid,),
            )
            diary["atoms"] = [{"id": a[0], "content": a[1], "type": a[2], "importance": a[3]} for a in atoms]

            # 关联全局事实
            facts = await self._fetch("""
                SELECT af.id, af.content, af.atom_type, af.importance, dfl.snippet
                FROM atomic_facts af
                JOIN diary_fact_links dfl ON dfl.fact_id = af.id
                WHERE dfl.diary_id = ?
            """, (eid,))
            diary["facts"] = [{"id": f[0], "content": f[1], "type": f[2], "importance": f[3], "snippet": f[4]} for f in facts]

            return self._ok(diary)
        except Exception as e:
            return self._error(str(e))

    async def update_memory(self):
        """更新日记条目"""
        try:
            from quart import request
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
            if updates:
                updates["updated_at"] = time.time()
                sets = ", ".join(f"{k}=?" for k in updates)
                vals = list(updates.values()) + [entry_id]
                await self._execute(f"UPDATE diary_entries SET {sets} WHERE id=?", vals)
            return self._ok({"updated": True, "new_memory_id": entry_id})
        except Exception as e:
            return self._error(str(e))

    async def _delete_single_diary(self, diary_id: int) -> int:
        exclusive = await self._fetch("""
            SELECT ma.id FROM memory_atoms ma
            WHERE ma.diary_id=? AND ma.status='active'
            AND (SELECT COUNT(*) FROM memory_atoms sub
                 WHERE sub.content=ma.content AND sub.user_id=ma.user_id
                 AND sub.status='active' AND sub.diary_id!=ma.diary_id) = 0
        """, (diary_id,))
        eids = [r[0] for r in exclusive]
        if eids:
            placeholders = ",".join("?" * len(eids))
            await self._execute(
                f"UPDATE memory_atoms SET status='forgotten' WHERE id IN ({placeholders})", eids
            )
        await self._execute("DELETE FROM diary_entries WHERE id=?", (diary_id,))
        return len(eids)

    async def delete_memory(self):
        try:
            from quart import request
            body = await request.get_json() or {}
            eid = body.get("memory_id") or body.get("id") or 0
            if not eid:
                return self._error("id is required")
            cleaned = await self._delete_single_diary(eid)
            return self._ok({"deleted": True, "cleaned_atoms": cleaned})
        except Exception as e:
            return self._error(str(e))

    async def batch_delete_memories(self):
        try:
            from quart import request
            body = await request.get_json() or {}
            ids = body.get("ids", [])
            if not ids:
                return self._error("ids is required")
            total_cleaned = 0
            for eid in ids:
                total_cleaned += await self._delete_single_diary(eid)
            return self._ok({"deleted": len(ids), "cleaned_atoms": total_cleaned})
        except Exception as e:
            return self._error(str(e))

    async def update_diary_status(self):
        try:
            from quart import request
            body = await request.get_json() or {}
            ids = body.get("ids", [])
            status = body.get("status", "active")
            if ids:
                ph = ",".join("?" * len(ids))
                await self._execute(
                    f"UPDATE diary_entries SET status=? WHERE id IN ({ph})", [status] + ids
                )
            return self._ok({"updated": True})
        except Exception as e:
            return self._error(str(e))

    async def get_timeline(self):
        try:
            from quart import request
            uid = request.args.get("uid", "")
            year = request.args.get("year", "")
            month = request.args.get("month", "")
            if not uid:
                return self._error("uid is required")
            if year and month:
                ym = f"{year}-{int(month):02d}"
                rows = await self._fetch(
                    "SELECT DISTINCT date FROM diary_entries WHERE user_id=? AND date LIKE ? ORDER BY date DESC",
                    (uid, f"{ym}%"),
                )
            elif year:
                rows = await self._fetch(
                    "SELECT DISTINCT date FROM diary_entries WHERE user_id=? AND date LIKE ? ORDER BY date DESC",
                    (uid, f"{year}%"),
                )
            else:
                rows = await self._fetch(
                    "SELECT DISTINCT date FROM diary_entries WHERE user_id=? ORDER BY date DESC LIMIT 100",
                    (uid,),
                )
            return self._ok([r[0] for r in rows])
        except Exception as e:
            return self._error(str(e))

    async def get_day_detail(self):
        try:
            from quart import request
            uid = request.args.get("uid", "")
            date = request.args.get("date", "")
            if not uid or not date:
                return self._error("uid and date required")
            date = self._parse_date(date)
            row = await self._fetchone(
                "SELECT * FROM diary_entries WHERE user_id=? AND date=? ORDER BY id DESC LIMIT 1",
                (uid, date),
            )
            if not row:
                return self._ok(None)
            columns = ["id", "uid", "user_id", "date", "timestamp", "content", "importance", "mood", "topics", "sentiment",
                       "fact_extracted", "fact_retry_count", "archived", "correction", "created_at"]
            diary = dict(zip(columns, row))
            atoms = await self._fetch(
                "SELECT id, content, atom_type, importance FROM memory_atoms WHERE diary_id=? AND status='active' ORDER BY importance DESC",
                (diary["id"],),
            )
            diary["atoms"] = [{"id": a[0], "content": a[1], "type": a[2], "importance": a[3]} for a in atoms]
            return self._ok(diary)
        except Exception as e:
            return self._error(str(e))

    async def get_diary(self):
        try:
            from quart import request
            eid = request.args.get("id", 0, type=int)
            uid = request.args.get("uid", "")
            date = request.args.get("date", "")
            if eid:
                row = await self._fetchone("SELECT content FROM diary_entries WHERE id=?", (eid,))
            elif uid and date:
                date = self._parse_date(date)
                row = await self._fetchone(
                    "SELECT content FROM diary_entries WHERE user_id=? AND date=? ORDER BY id DESC LIMIT 1",
                    (uid, date),
                )
            else:
                return self._error("id or uid+date required")
            content = row[0] if row else ""
            return self._ok({"content": content})
        except Exception as e:
            return self._error(str(e))

    async def update_diary(self):
        try:
            from quart import request
            from ..core.diary_helper import parse_diary_content
            body_req = await request.get_json()
            user_id = body_req.get("user_id", "Hana")
            date = body_req.get("date", "")
            content = body_req.get("content", "")
            await self.core.diary_store.upsert(user_id, date, content)
            fm, _ = parse_diary_content(content)
            updates = {}
            if "mood" in fm:
                from ..core.diary_helper import mood_to_sentiment
                updates["sentiment"] = mood_to_sentiment(str(fm["mood"]))
            if "importance" in fm:
                updates["importance"] = float(fm["importance"])
            if "topics" in fm:
                topics = fm["topics"]
                if isinstance(topics, list):
                    updates["topics"] = json.dumps(topics, ensure_ascii=False)
            if updates:
                await self.core.diary_store.update_metadata(user_id, date, **updates)
            return self._ok({"saved": True})
        except Exception as e:
            return self._error(str(e))

    async def get_persona(self):
        try:
            from quart import request
            uid = request.args.get("uid", "")
            if not uid:
                return self._error("uid is required")
            row = await self._fetchone(
                "SELECT summary, full_markdown, tags FROM user_persona WHERE uid=?", (uid,)
            )
            if not row:
                return self._ok({"summary": "", "full_markdown": "", "tags": []})
            tags = []
            if row[2]:
                try:
                    tags = json.loads(row[2]) if isinstance(row[2], str) else row[2]
                except Exception:
                    tags = []
            return self._ok({"summary": row[0] or "", "full_markdown": row[1] or "", "tags": tags})
        except Exception as e:
            return self._error(str(e))

    async def update_persona(self):
        try:
            from quart import request
            body = await request.get_json() or {}
            uid = body.get("uid", "")
            if not uid:
                return self._error("uid is required")
            summary = body.get("summary", "")
            full_md = body.get("full_markdown", "")
            tags = body.get("tags", [])
            tags_json = json.dumps(tags, ensure_ascii=False) if isinstance(tags, list) else tags
            await self._execute(
                "UPDATE user_persona SET summary=?, full_markdown=?, tags=? WHERE uid=?",
                (summary, full_md, tags_json, uid),
            )
            return self._ok({"saved": True})
        except Exception as e:
            return self._error(str(e))

    async def list_users(self):
        try:
            rows = await self._fetch("""
                SELECT cp.uid, cp.primary_name, cp.identity_confidence,
                       up.tier, up.summary, up.last_full_update
                FROM canonical_users cp
                LEFT JOIN user_persona up ON cp.uid = up.uid
                ORDER BY up.last_full_update DESC
            """)
            users = []
            for r in rows:
                users.append({
                    "uid": r[0], "name": r[1] or r[0], "identity_confidence": r[2],
                    "tier": r[3] or "new", "summary": (r[4] or "")[:100],
                    "last_active": r[5],
                })
            return self._ok(users)
        except Exception as e:
            return self._error(str(e))

    async def get_user_detail(self):
        try:
            from quart import request
            uid = request.args.get("uid", "")
            if not uid:
                return self._error("uid is required")
            row = await self._fetchone("SELECT * FROM user_persona WHERE uid=?", (uid,))
            if not row:
                return self._ok(None)
            cols = ["uid", "summary", "full_markdown", "tags", "version", "tier",
                    "last_full_update", "last_incremental_update", "known_ids", "primary_name",
                    "identity_confidence", "incremental_count", "diary_count_since_full",
                    "created_at", "updated_at"]
            return self._ok(dict(zip(cols, row)))
        except Exception as e:
            return self._error(str(e))

    async def list_archived(self):
        try:
            from quart import request
            uid = request.args.get("uid", "")
            page = int(request.args.get("page", 1))
            size = int(request.args.get("size", 20))
            offset = (page - 1) * size
            if uid:
                rows = await self._fetch(
                    "SELECT id, user_id, date, importance FROM diary_entries WHERE user_id=? AND archived=1 ORDER BY date DESC LIMIT ? OFFSET ?",
                    (uid, size, offset),
                )
                total = (await self._fetchone("SELECT COUNT(*) FROM diary_entries WHERE user_id=? AND archived=1", (uid,)))[0]
            else:
                rows = await self._fetch(
                    "SELECT id, user_id, date, importance FROM diary_entries WHERE archived=1 ORDER BY date DESC LIMIT ? OFFSET ?",
                    (size, offset),
                )
                total = (await self._fetchone("SELECT COUNT(*) FROM diary_entries WHERE archived=1"))[0]
            items = [{"id": r[0], "user_id": r[1], "date": r[2], "importance": r[3]} for r in rows]
            return self._ok({"items": items, "total": total, "page": page, "size": size})
        except Exception as e:
            return self._error(str(e))

    async def restore_archived(self):
        try:
            from quart import request
            body = await request.get_json() or {}
            ids = body.get("ids", [])
            if ids:
                ph = ",".join("?" * len(ids))
                await self._execute(
                    f"UPDATE diary_entries SET archived=0 WHERE id IN ({ph})", ids
                )
            return self._ok({"restored": len(ids)})
        except Exception as e:
            return self._error(str(e))

    async def import_livingmemory(self):
        """从旧版 livingmemory 导入（保留兼容调用方式）"""
        from ..scripts.import_livingmemory import import_livingmemory_db
        try:
            from quart import request
            body = await request.get_json() or {}
            source = body.get("source", "/home/hako/data/plugin_data/astrbot_plugin_livingmemory/livingmemory.db")
            result = await import_livingmemory_db(
                source_db=source,
                target_db=str(self.core.data_dir / "memory.db"),
                atom_store=self.core.atom_store,
                diary_store=self.core.diary_store,
            )
            return self._ok(result)
        except Exception as e:
            logger.warning(f"[Memory] 导入 livingmemory 失败: {e}")
            return self._error(str(e))

    @staticmethod
    async def _get_json():
        """获取请求体 JSON（兼容不同 Web 框架）"""
        from quart import request
        return await request.get_json()
