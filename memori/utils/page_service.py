"""WebUI Dashboard Service — 纯业务逻辑，零框架依赖

所有方法接受显式参数，返回 dict。
不导入任何 Web 框架（Quart / FastAPI / etc）。
"""

from __future__ import annotations

import json
import time
from typing import Any

from ..core.logger import logger
from .context_formatter import fmt_ts


class PageService:
    """Memory 插件 Dashboard 业务逻辑

    所有数据库操作通过 MemoryCore 的 store 实例执行。
    可独立于任何 Web 框架进行测试。
    """

    def __init__(self, memory_core):
        self.core = memory_core

    # ── Store 快捷引用 ──

    @property
    def _db(self):
        return self.core.atom_store

    @property
    def _db_diary(self):
        return self.core.diary_store

    @property
    def _db_graph(self):
        return self.core.graph_store

    # ── 响应辅助 ──

    @staticmethod
    def _ok(data: Any) -> dict:
        return {"ok": True, "data": data}

    @staticmethod
    def _error(msg: str) -> dict:
        return {"ok": False, "error": msg}

    @staticmethod
    def _parse_date(d: str) -> str:
        d = d.replace("/", "-")
        parts = d.split("-")
        if len(parts) == 3:
            return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
        return d

    # ═══════════════════════════════════════════════════
    #  API 逻辑
    # ═══════════════════════════════════════════════════

    async def get_stats(self) -> dict:
        """概览统计"""
        try:
            user_count = (await self._db.fetchone("SELECT COUNT(DISTINCT uid) FROM user_persona"))[0] or 0
            diary_count = (await self._db_diary.fetchone("SELECT COUNT(*) FROM diary_entries"))[0] or 0
            atom_count = (await self._db.fetchone("SELECT COUNT(*) FROM memory_atoms WHERE status='active'"))[0] or 0
            node_count = (await self._db_graph.fetchone("SELECT COUNT(*) FROM nodes"))[0] or 0
            edge_count = (await self._db_graph.fetchone("SELECT COUNT(*) FROM edges WHERE status='active'"))[0] or 0
            return self._ok({
                "users": user_count, "diaries": diary_count,
                "atoms": atom_count,
                "graph_nodes": node_count, "graph_edges": edge_count,
            })
        except Exception as e:
            return self._error(str(e))

    async def get_graph_overview(self) -> dict:
        """图谱概览"""
        try:
            rows = await self._db_graph.fetch("SELECT type, COUNT(*) FROM nodes GROUP BY type")
            nodes = {r[0]: r[1] for r in rows}
            rel_rows = await self._db_graph.fetch(
                "SELECT relation_type, COUNT(*) FROM edges WHERE status='active' GROUP BY relation_type"
            )
            edges = {r[0]: r[1] for r in rel_rows}
            return self._ok({"nodes": nodes, "edges": edges})
        except Exception as e:
            return self._error(str(e))

    async def query_graph(self, entity: str) -> dict:
        """图查询"""
        if not entity or not self.core.graph_engine:
            return self._ok({"nodes": [], "edges": []})
        try:
            result = await self.core.graph_engine.query_neighbors(entity)
            return self._ok(result)
        except Exception as e:
            return self._error(str(e))

    async def list_memories(self, page: int = 1, size: int = 20) -> dict:
        """日记列表（分页，全库），按创建时间降序"""
        try:
            offset = (page - 1) * size
            rows = await self._db_diary.fetch(
                "SELECT id, date, importance, sentiment, topics, created_at, updated_at "
                "FROM diary_entries ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (size, offset),
            )
            total = (await self._db_diary.fetchone("SELECT COUNT(*) FROM diary_entries"))[0]

            items = []
            for r in rows:
                topics_raw = r[4]
                topics = []
                if topics_raw:
                    try:
                        topics = json.loads(topics_raw) if isinstance(topics_raw, str) else topics_raw
                    except Exception:
                        topics = [str(topics_raw)]
                items.append({
                    "id": r[0], "date": r[1],
                    "importance": r[2], "sentiment": r[3], "topics": topics,
                    "created_at": fmt_ts(r[5]), "updated_at": fmt_ts(r[6]),
                })
            return self._ok({"items": items, "total": total, "page": page, "size": size})
        except Exception as e:
            return self._error(str(e))

    async def get_memory_detail(self, entry_id: int) -> dict:
        """日记详情"""
        try:
            diary = await self.core.diary_store.get_by_id(entry_id)
            if not diary:
                return self._error("not found")
            diary['created_at'] = fmt_ts(diary['created_at'])
            atoms = await self._db.fetch(
                "SELECT a.id, a.content, a.atom_type, a.importance FROM memory_atoms a "
                "JOIN atoms_diary_links d ON a.id = d.atom_id "
                "WHERE d.diary_id=? AND a.status='active' ORDER BY d.importance DESC",
                (entry_id,),
            )
            diary["atoms"] = [{"id": a[0], "content": a[1], "type": a[2], "importance": a[3]} for a in atoms]
            return self._ok(diary)
        except Exception as e:
            return self._error(str(e))

    async def update_memory(self, entry_id: int, updates: dict) -> dict:
        """更新日记条目"""
        try:
            if not updates:
                return self._ok({"updated": False})
            updates["updated_at"] = time.time()
            sets = ", ".join(f"{k}=?" for k in updates)
            vals = list(updates.values()) + [entry_id]
            await self._db_diary.execute(f"UPDATE diary_entries SET {sets} WHERE id=?", vals)
            return self._ok({"updated": True})
        except Exception as e:
            return self._error(str(e))

    async def _delete_single_diary(self, diary_id: int) -> int:
        """删除单条日记及其独占原子"""
        exclusive = await self._db.fetch("""
            SELECT ma.id FROM memory_atoms ma
            JOIN atoms_diary_links d ON ma.id = d.atom_id
            WHERE d.diary_id=? AND ma.status='active'
            AND (SELECT COUNT(*) FROM atoms_diary_links sub
                 WHERE sub.atom_id=ma.id AND sub.diary_id!=?) = 0
        """, (diary_id, diary_id))
        eids = [r[0] for r in exclusive]
        if eids:
            ph = ",".join("?" * len(eids))
            await self._db.execute(
                f"UPDATE memory_atoms SET status='forgotten' WHERE id IN ({ph})", eids
            )
        await self._db_diary.execute("DELETE FROM diary_entries WHERE id=?", (diary_id,))
        return len(eids)

    async def delete_memory(self, entry_id: int) -> dict:
        """删除日记"""
        try:
            if not entry_id:
                return self._error("id is required")
            cleaned = await self._delete_single_diary(entry_id)
            return self._ok({"deleted": True, "cleaned_atoms": cleaned})
        except Exception as e:
            return self._error(str(e))

    async def batch_delete_memories(self, ids: list[int]) -> dict:
        """批量删除"""
        try:
            if not ids:
                return self._error("ids is required")
            total = 0
            for eid in ids:
                total += await self._delete_single_diary(eid)
            return self._ok({"deleted": len(ids), "cleaned_atoms": total})
        except Exception as e:
            return self._error(str(e))

    async def update_diary_status(self, ids: list[int], status: str = "active") -> dict:
        """批量更新日记状态"""
        try:
            if ids:
                ph = ",".join("?" * len(ids))
                await self._db_diary.execute(
                    f"UPDATE diary_entries SET status=? WHERE id IN ({ph})", [status] + ids
                )
            return self._ok({"updated": True})
        except Exception as e:
            return self._error(str(e))

    async def get_timeline(self, year: str = "", month: str = "") -> dict:
        """记忆时间线（全库）"""
        try:
            if year and month:
                ym = f"{year}-{int(month):02d}"
                rows = await self._db_diary.fetch(
                    "SELECT DISTINCT date FROM diary_entries WHERE date LIKE ? ORDER BY date DESC",
                    (f"{ym}%",),
                )
            elif year:
                rows = await self._db_diary.fetch(
                    "SELECT DISTINCT date FROM diary_entries WHERE date LIKE ? ORDER BY date DESC",
                    (f"{year}%",),
                )
            else:
                rows = await self._db_diary.fetch(
                    "SELECT DISTINCT date FROM diary_entries ORDER BY date DESC LIMIT 100"
                )
            return self._ok([r[0] for r in rows])
        except Exception as e:
            return self._error(str(e))

    async def get_day_detail(self, date: str) -> dict:
        """获取指定日期日记"""
        try:
            if not date:
                return self._error("date required")
            date = self._parse_date(date)
            row = await self._db_diary.fetchone(
                "SELECT id FROM diary_entries WHERE date=? ORDER BY id DESC LIMIT 1",
                (date,),
            )
            if not row:
                return self._ok(None)
            diary = await self.core.diary_store.get_by_id(row[0])
            if not diary:
                return self._ok(None)
            diary['created_at'] = fmt_ts(diary['created_at'])
            atoms = await self._db.fetch(
                "SELECT a.id, a.content, a.atom_type, a.importance FROM memory_atoms a "
                "JOIN atoms_diary_links d ON a.id = d.atom_id "
                "WHERE d.diary_id=? AND a.status='active' ORDER BY d.importance DESC",
                (diary["id"],),
            )
            diary["atoms"] = [{"id": a[0], "content": a[1], "type": a[2], "importance": a[3]} for a in atoms]
            return self._ok(diary)
        except Exception as e:
            return self._error(str(e))

    async def get_diary(self, entry_id: int = 0, date: str = "") -> dict:
        """获取日记内容"""
        try:
            if entry_id:
                row = await self._db_diary.fetchone("SELECT content FROM diary_entries WHERE id=?", (entry_id,))
            elif date:
                date = self._parse_date(date)
                row = await self._db_diary.fetchone(
                    "SELECT content FROM diary_entries WHERE date=? ORDER BY id DESC LIMIT 1",
                    (date,),
                )
            else:
                return self._error("id or date required")
            content = row[0] if row else ""
            return self._ok({"content": content})
        except Exception as e:
            return self._error(str(e))

    async def update_diary(self, date: str, content: str) -> dict:
        """更新日记"""
        try:
            from ..utils.diary_helper import parse_diary_content, mood_to_sentiment
            await self.core.diary_store.upsert(date, content)
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
                await self.core.diary_store.update_metadata(date, **updates)
            return self._ok({"saved": True})
        except Exception as e:
            return self._error(str(e))

    async def get_persona(self, uid: str) -> dict:
        """获取用户画像"""
        try:
            if not uid:
                return self._error("uid is required")
            row = await self._db.fetchone(
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

    async def update_persona(self, uid: str, summary: str, full_md: str, tags: list) -> dict:
        """更新用户画像"""
        try:
            tags_json = json.dumps(tags, ensure_ascii=False) if isinstance(tags, list) else tags
            await self._db.execute(
                "UPDATE user_persona SET summary=?, full_markdown=?, tags=? WHERE uid=?",
                (summary, full_md, tags_json, uid),
            )
            return self._ok({"saved": True})
        except Exception as e:
            return self._error(str(e))

    async def list_users(self) -> dict:
        """用户列表"""
        try:
            rows = await self._db.fetch("""
                SELECT cp.uid, cp.primary_name, cp.identity_confidence,
                       up.tier, up.summary, up.last_full_update
                FROM canonical_users cp
                LEFT JOIN user_persona up ON cp.uid = up.uid
                ORDER BY up.last_full_update DESC
            """)
            users = [{
                "uid": r[0], "name": r[1] or r[0], "identity_confidence": r[2],
                "tier": r[3] or "new", "summary": (r[4] or "")[:100],
                "last_active": r[5],
            } for r in rows]
            return self._ok(users)
        except Exception as e:
            return self._error(str(e))

    async def get_user_detail(self, uid: str) -> dict:
        """用户详情"""
        try:
            if not uid:
                return self._error("uid is required")
            row = await self._db.fetchone("SELECT * FROM user_persona WHERE uid=?", (uid,))
            if not row:
                return self._ok(None)
            cols = ["uid", "summary", "full_markdown", "tags", "version", "tier",
                    "last_full_update", "last_incremental_update", "known_ids", "primary_name",
                    "identity_confidence", "incremental_count", "diary_count_since_full",
                    "created_at", "updated_at"]
            d = dict(zip(cols, row))
            d["created_at"] = fmt_ts(d["created_at"])
            d["updated_at"] = fmt_ts(d["updated_at"])
            return self._ok(d)
        except Exception as e:
            return self._error(str(e))

    async def list_archived(self, page: int = 1, size: int = 20) -> dict:
        """归档列表（全库）"""
        try:
            offset = (page - 1) * size
            rows = await self._db_diary.fetch(
                "SELECT id, date, importance FROM diary_entries "
                "WHERE archived=1 ORDER BY date DESC LIMIT ? OFFSET ?",
                (size, offset),
            )
            total = (await self._db_diary.fetchone(
                "SELECT COUNT(*) FROM diary_entries WHERE archived=1"
            ))[0]
            items = [{"id": r[0], "date": r[1], "importance": r[2]} for r in rows]
            return self._ok({"items": items, "total": total, "page": page, "size": size})
        except Exception as e:
            return self._error(str(e))

    async def restore_archived(self, ids: list[int]) -> dict:
        """恢复归档"""
        try:
            if ids:
                ph = ",".join("?" * len(ids))
                await self._db_diary.execute(
                    f"UPDATE diary_entries SET archived=0 WHERE id IN ({ph})", ids
                )
            return self._ok({"restored": len(ids)})
        except Exception as e:
            return self._error(str(e))
