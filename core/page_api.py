"""WebUI Dashboard API — 注册 AstrBot 原生页面 API"""

from __future__ import annotations

import json
import time
from typing import Any, TYPE_CHECKING

from .logger import logger

if TYPE_CHECKING:
    from ..core.memory_core import MemoryCore


class PageApi:
    """Memory 插件 Dashboard API

    优化：所有数据库操作通过 Store 层的异步连接池执行，
    不再创建独立的 sync sqlite3 连接。
    """

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
        register(f"{prefix}/memories/batch-update", self.update_diary_status, ["POST"], "Batch update diary (alias)")
        register(f"{prefix}/memories/timeline", self.get_timeline, ["GET"], "Memory timeline")
        register(f"{prefix}/memories/day", self.get_day_detail, ["GET"], "Memory day detail")
        register(f"{prefix}/diary", self.get_diary, ["GET"], "Get diary content")
        register(f"{prefix}/diary/update", self.update_diary, ["POST"], "Update diary")
        register(f"{prefix}/persona", self.get_persona, ["GET"], "Get persona")
        register(f"{prefix}/persona/update", self.update_persona, ["POST"], "Update persona")
        register(f"{prefix}/import/livingmemory", self.import_livingmemory, ["POST"], "Import from livingmemory")
        register(f"{prefix}/providers", self.list_providers, ["GET"], "List LLM providers")
        register(f"{prefix}/users", self.list_users, ["GET"], "List users")
        register(f"{prefix}/users/detail", self.get_user_detail, ["GET"], "User detail with memories")
        register(f"{prefix}/archive/list", self.list_archived, ["GET"], "List archived entries")
        register(f"{prefix}/archive/restore", self.restore_archived, ["POST"], "Restore from archive")

    # ── 复用 Store 的异步连接池 ──

    @property
    def _db(self):
        """获取 atom_store 的底层 DB 连接（共享连接池）"""
        return self.core.atom_store

    async def _fetch(self, sql: str, params: tuple | list | None = None) -> list:
        """通过共享连接池执行异步查询"""
        return await self._db.fetch(sql, params)

    async def _fetchone(self, sql: str, params: tuple | list | None = None):
        """取单行"""
        return await self._db.fetchone(sql, params)

    async def _execute(self, sql: str, params: tuple | list | None = None):
        """通过共享连接池执行异步写入"""
        return await self._db.execute(sql, params)

    # ── API 处理 ──

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

            default_uid = self.core.config.get("default_user_id", "")
            atom_stats = await atom_store.get_stats(default_uid) if default_uid else {"total": 0, "by_type": {}}
            diary_dates = await diary_store.list_months(default_uid) if default_uid else []
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
        """图谱概览（兼容 LM 前端格式）"""
        try:
            raw = await self.core.graph_store.get_full_graph(500)
            return self._ok({
                "enabled": True,
                "mode": "overview",
                "snapshot": {
                    "nodes": raw.get("nodes", []),
                    "edges": raw.get("edges", []),
                },
                "total_nodes": len(raw.get("nodes", [])),
                "total_edges": len(raw.get("edges", [])),
                "by_type": {},
            })
        except Exception as e:
            return self._error(str(e))

    async def query_graph(self):
        """搜索图谱（兼容 LM 前端格式）"""
        try:
            from quart import request
            body = await request.get_json()
            query = (body or {}).get("query", "")
            raw = await self.core.graph_store.query_graph(query, 100)
            return self._ok({
                "enabled": True,
                "mode": "query",
                "snapshot": {
                    "nodes": raw.get("nodes", []),
                    "edges": raw.get("edges", []),
                },
            })
        except Exception as e:
            return self._error(str(e))

    async def list_memories(self):
        """列出所有日记（分页 + 过滤 + 原子统计）"""
        try:
            from quart import request
            q = request.args
            keyword = q.get("keyword", "").strip()
            user_id = q.get("user_id", "all")
            year = q.get("year", "")
            month = q.get("month", "")
            page = max(1, int(q.get("page", 1)))
            page_size = min(200, max(1, int(q.get("page_size", 50))))

            conditions = []
            params: list = []
            if user_id and user_id != "all":
                conditions.append("d.user_id = ?")
                params.append(user_id)
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

            where_sql = (" WHERE " + " AND ".join(conditions)) if conditions else ""
            rows = await self._fetch(f"""
                SELECT d.id, d.date, d.content, d.created_at, d.updated_at, COALESCE(d.status,'active'),
                       (SELECT COUNT(*) FROM memory_atoms a WHERE a.diary_date=d.date AND a.user_id=d.user_id AND a.status='active'),
                       d.importance
                FROM diary_entries d{where_sql}
                ORDER BY d.id DESC LIMIT ? OFFSET ?
            """, params + [page_size, (page - 1) * page_size])

            total_row = await self._fetch(
                f"SELECT COUNT(*) FROM diary_entries d{where_sql}", params
            )
            total = total_row[0][0] if total_row else 0

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
                    "types": await self._get_atom_types_for_date(dt, did),
                })
            return self._ok({"total": total, "page": page, "page_size": page_size, "items": items})
        except Exception as e:
            return self._error(str(e))

    async def _get_atom_types_for_date(self, date_str: str, diary_id: int = 0) -> list:
        """获取某日记下原子的类型分布"""
        try:
            if diary_id:
                rows = await self._fetch(
                    "SELECT atom_type, COUNT(*) FROM memory_atoms WHERE diary_id=? AND status='active' GROUP BY atom_type ORDER BY COUNT(*) DESC",
                    (diary_id,),
                )
            else:
                rows = await self._fetch(
                    "SELECT atom_type, COUNT(*) FROM memory_atoms WHERE diary_date=? AND status='active' GROUP BY atom_type ORDER BY COUNT(*) DESC",
                    (date_str,),
                )
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
        """更新日记条目

        兼容两种入参格式：
        - 前端格式：{ memory_id, field: "content"|"status"|"importance", value }
        - 旧格式：{ id, content/importance/status 直接字段 }
        """
        try:
            from quart import request
            body = await request.get_json() or {}
            entry_id = body.get("memory_id") or body.get("id") or 0
            updates = {}

            # 旧格式兼容：直接字段模式
            for f in ("content", "importance", "status"):
                if f in body:
                    updates[f] = body[f]

            # 前端格式：field + value 模式
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
                await self._execute(
                    f"UPDATE diary_entries SET {sets} WHERE id=?", vals
                )

            return self._ok({"updated": True, "new_memory_id": entry_id})
        except Exception as e:
            return self._error(str(e))

    async def delete_memory(self):
        """删除日记条目，仅清理其独占的原子事实

        规则：原子只关联了这篇日记 → 一起删
              原子还被其他日记关联着 → 保留（只去掉本日记的关联）
        """
        try:
            from quart import request
            body = await request.get_json()
            diary_id = body.get("id", 0)
            if not diary_id:
                return self._error("id required")

            # 找出被本篇日记独占的原子（仅此一篇日记引用）
            exclusive = await self._fetch("""
                SELECT ma.id FROM memory_atoms ma
                WHERE ma.diary_id=? AND ma.status='active'
                AND (SELECT COUNT(*) FROM memory_atoms sub
                     WHERE sub.content=ma.content AND sub.user_id=ma.user_id
                     AND sub.status='active' AND sub.diary_id!=ma.diary_id) = 0
            """, (diary_id,))

            exclusive_ids = [r[0] for r in exclusive]

            # 从日记中删除原子关联
            await self._execute(
                "UPDATE memory_atoms SET status='forgotten' WHERE id IN ({})".format(
                    ",".join("?" * len(exclusive_ids))
                ) if exclusive_ids else "SELECT 1 WHERE 0",
                exclusive_ids,
            )

            # 删除日记本身
            await self._execute("DELETE FROM diary_entries WHERE id=?", (diary_id,))

            return self._ok({"deleted": True, "exclusive_atoms": len(exclusive_ids)})
        except Exception as e:
            return self._error(str(e))

    async def batch_delete_memories(self):
        """批量删除日记（仅清理独占原子）"""
        try:
            from quart import request
            body = await request.get_json()
            ids = body.get("ids", [])

            total_exclusive = 0
            for did in ids:
                exclusive = await self._fetch("""
                    SELECT ma.id FROM memory_atoms ma
                    WHERE ma.diary_id=? AND ma.status='active'
                    AND (SELECT COUNT(*) FROM memory_atoms sub
                         WHERE sub.content=ma.content AND sub.user_id=ma.user_id
                         AND sub.status='active' AND sub.diary_id!=ma.diary_id) = 0
                """, (did,))

                eids = [r[0] for r in exclusive]
                if eids:
                    await self._execute(
                        "UPDATE memory_atoms SET status='forgotten' WHERE id IN ({})".format(
                            ",".join("?" * len(eids))
                        ), eids,
                    )
                    total_exclusive += len(eids)

                await self._execute("DELETE FROM diary_entries WHERE id=?", (did,))

            return self._ok({"deleted": len(ids), "exclusive_atoms": total_exclusive})
        except Exception as e:
            return self._error(str(e))

    async def update_diary_status(self):
        """更新日记状态（单条 + 批量双模式）

        单条：{ id, status }
        批量：{ memory_ids: [...], field: "status", value: "archived" }
        """
        try:
            from quart import request
            body = await request.get_json() or {}

            # 批量模式 — 前端 batch-update 调用
            memory_ids = body.get("memory_ids") or []
            if memory_ids:
                field = body.get("field", "status")
                value = body.get("value", "active")
                now = time.time()
                for eid in memory_ids:
                    await self._execute(
                        f"UPDATE diary_entries SET {field}=?, updated_at=? WHERE id=?",
                        (value, now, eid),
                    )
                return self._ok({"updated": len(memory_ids)})

            # 单条模式
            diary_id = body.get("id", 0)
            status_val = body.get("status", "active")
            await self._execute(
                "UPDATE diary_entries SET status=?, updated_at=? WHERE id=?",
                (status_val, time.time(), diary_id),
            )
            return self._ok({"updated": True})
        except Exception as e:
            return self._error(str(e))

    async def get_timeline(self):
        """按时间线列出所有记忆"""
        try:
            from quart import request
            q = request.args
            user_id = q.get("user_id", "all")
            page = max(1, int(q.get("page", 1)))
            page_size = min(100, max(1, int(q.get("page_size", 20))))
            data = await self.core.atom_store.get_timeline(user_id, page, page_size)
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

            # 读日记
            diary = await self._fetch(
                "SELECT id, date, content, topics, sentiment, status FROM diary_entries WHERE id=?",
                (did,),
            )
            if not diary:
                return self._error("未找到该日记")

            row = diary[0]
            did_val, date_str, content, topics, sentiment, status = row
            status = status or "active"

            # 读该日记关联的事实（优先 atomic_facts 表，旧数据回退 memory_atoms）
            atoms = []
            new_rows = await self._fetch("""
                SELECT af.id, af.content, af.atom_type, dfl.importance, dfl.snippet
                FROM atomic_facts af
                JOIN diary_fact_links dfl ON af.id = dfl.fact_id
                WHERE dfl.diary_id = ?
                ORDER BY dfl.importance DESC
            """, (did,))
            if new_rows:
                for r in new_rows:
                    atoms.append({
                        "id": r[0], "content": r[1], "type": r[2],
                        "importance": r[3], "snippet": r[4] or "",
                    })
            else:
                atom_rows = await self._fetch("""
                    SELECT id, content, atom_type, importance, diary_snippet
                    FROM memory_atoms WHERE diary_id=? AND status='active'
                    ORDER BY importance DESC
                """, (did,))
                for r in atom_rows:
                    atoms.append({
                        "id": r[0], "content": r[1], "type": r[2],
                        "importance": r[3], "snippet": r[4] or "",
                    })

            imp_stats = {"avg": 0, "max": 0, "count": len(atoms)}
            if atoms:
                imps = [a["importance"] for a in atoms]
                imp_stats["avg"] = round(sum(imps) / len(imps), 2)
                imp_stats["max"] = max(imps)

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
        """获取日记内容（返回解析后的 frontmatter + body）"""
        try:
            from quart import request
            user_id = request.args.get("user_id", "Hana")
            date = request.args.get("date", "")
            content = await self.core.diary_store.read(user_id, date)
            if not content:
                return self._ok({"date": date, "content": "", "frontmatter": {}, "body": ""})
            from ..core.diary_helper import parse_diary_content
            fm, body = parse_diary_content(content)
            return self._ok({
                "date": date,
                "content": content,
                "frontmatter": fm,
                "body": body,
            })
        except Exception as e:
            return self._error(str(e))

    async def update_diary(self):
        """更新日记（保存 content，同步 frontmatter 到 DB 字段）"""
        try:
            from quart import request
            from ..core.diary_helper import parse_diary_content
            body_req = await request.get_json()
            user_id = body_req.get("user_id", "Hana")
            date = body_req.get("date", "")
            content = body_req.get("content", "")

            await self.core.diary_store.upsert(user_id, date, content)

            # 同步 frontmatter → DB 字段
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
                    import json
                    updates["topics"] = json.dumps(topics, ensure_ascii=False)
            if updates:
                await self.core.diary_store.update_metadata(user_id, date, **updates)

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

    # ═══════════════════════════════════════════════════
    #  用户管理
    # ═══════════════════════════════════════════════════

    async def list_users(self):
        """列出所有规范用户及画像"""
        try:
            rows = await self._db.fetch("""
                SELECT cu.uid, cu.primary_name, cu.identity_confidence,
                       up.summary, up.tags, up.tier, up.version,
                       up.last_incremental_update, up.last_full_update,
                       (SELECT COUNT(*) FROM user_identities ui WHERE ui.uid=cu.uid) as id_count
                FROM canonical_users cu
                LEFT JOIN user_persona up ON cu.uid = up.uid
                ORDER BY up.last_incremental_update DESC NULLS LAST
            """)
            import json
            users = []
            for r in rows:
                tags = []
                try:
                    tags = json.loads(r[4]) if r[4] else []
                except Exception:
                    pass
                users.append({
                    "uid": r[0], "name": r[1] or r[0], "confidence": r[2],
                    "summary": (r[3] or "")[:300], "tags": tags,
                    "tier": r[5] or "new", "version": r[6] or 0,
                    "last_update": r[7] or r[8] or 0,
                    "identity_count": r[9] or 0,
                })
            return self._ok(users)
        except Exception as e:
            return self._error(str(e))

    async def get_user_detail(self):
        """获取用户详情（画像 + 关联记忆）"""
        try:
            from quart import request
            uid = request.args.get("uid", "")
            if not uid:
                return self._error("uid required")
            row = await self._db.fetchone("""
                SELECT cu.uid, cu.primary_name, cu.identity_confidence,
                       up.summary, up.full_markdown, up.tags, up.tier,
                       up.version, up.last_incremental_update, up.last_full_update,
                       up.diary_count_since_full, up.incremental_count
                FROM canonical_users cu
                LEFT JOIN user_persona up ON cu.uid = up.uid
                WHERE cu.uid=?
            """, (uid,))
            if not row:
                return self._error("未找到用户")
            import json
            tags = []
            try:
                tags = json.loads(row[5]) if row[5] else []
            except Exception:
                pass
            # 获取关联身份
            identities = await self._db.fetch(
                "SELECT platform, display_name, first_seen, verified FROM user_identities WHERE uid=?",
                (uid,),
            )
            # 获取最近的原子
            user_id_like = f"%{uid}%"
            atoms = await self._db.fetch("""
                SELECT content, atom_type, importance, diary_date
                FROM memory_atoms WHERE user_id LIKE ? AND status='active'
                ORDER BY created_at DESC LIMIT 20
            """, (user_id_like,))
            return self._ok({
                "uid": row[0], "name": row[1], "confidence": row[2],
                "summary": row[3] or "", "full_markdown": row[4] or "",
                "tags": tags, "tier": row[6] or "new",
                "version": row[7] or 0, "last_update": row[8] or row[9] or 0,
                "diary_count_since_full": row[10] or 0, "incremental_count": row[11] or 0,
                "identities": [{"platform": r[0], "name": r[1], "since": r[2], "verified": r[3]} for r in identities] if identities else [],
                "recent_atoms": [{"content": r[0], "type": r[1], "importance": r[2], "date": r[3]} for r in atoms] if atoms else [],
            })
        except Exception as e:
            return self._error(str(e))

    # ═══════════════════════════════════════════════════
    #  归档管理
    # ═══════════════════════════════════════════════════

    async def list_archived(self):
        """列出已归档日记"""
        try:
            from quart import request
            q = request.args
            keyword = q.get("keyword", "").strip()
            page = max(1, int(q.get("page", 1)))
            page_size = min(100, max(1, int(q.get("page_size", 20))))

            conditions = ["archived = 1"]
            params: list = []
            if keyword:
                conditions.append("content LIKE ?")
                params.append(f"%{keyword}%")

            where = " AND ".join(conditions)
            rows = await self._fetch(f"""
                SELECT id, user_id, date, content, importance, created_at
                FROM diary_entries WHERE {where}
                ORDER BY created_at DESC LIMIT ? OFFSET ?
            """, params + [page_size, (page - 1) * page_size])
            total = (await self._fetchone(
                f"SELECT COUNT(*) FROM diary_entries WHERE {where}", params
            ))[0]

            items = []
            for r in rows:
                items.append({
                    "id": r[0], "user_id": r[1], "date": r[2],
                    "content": (r[3] or "")[:200],
                    "importance": r[4], "created_at": r[5],
                })
            return self._ok({"total": total, "items": items, "page": page})
        except Exception as e:
            return self._error(str(e))

    async def restore_archived(self):
        """从归档恢复日记"""
        try:
            from quart import request
            body = await request.get_json()
            diary_id = body.get("id", 0)
            if not diary_id:
                return self._error("id required")
            if not hasattr(self.core, 'archiver') or not self.core.archiver:
                return self._error("归档模块不可用")
            restored = await self.core.archiver.restore_from_archive(diary_id)
            if restored:
                await self._execute(
                    "UPDATE diary_entries SET content=?, archived=0, updated_at=? WHERE id=?",
                    (restored, time.time(), diary_id),
                )
                return self._ok({"restored": True, "content": restored[:200]})
            return self._error("未找到归档内容")
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
