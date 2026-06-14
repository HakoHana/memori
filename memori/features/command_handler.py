"""指令处理器 — 处理用户命令"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ..core.logger import logger

from ..storage.diary_store import DiaryStore
from ..storage.atom_store import AtomStore
from ..storage.persona_store import PersonaStore
from ..core.interfaces import ICommandHandler, IRetriever, ICapturer


class CommandHandler(ICommandHandler):
    """处理 /日记、/记忆、/记忆 搜索 等指令"""

    def __init__(
        self,
        diary_store: DiaryStore,
        atom_store: AtomStore,
        persona_store: PersonaStore,
        retriever: IRetriever,
        capturer: ICapturer | None = None,
    ):
        self.diary_store = diary_store
        self.atom_store = atom_store
        self.persona_store = persona_store
        self.retriever = retriever
        self.capturer = capturer

    async def handle_diary(self, user_id: str, args: list[str]) -> str:
        """处理 /日记 [日期]"""
        date_str = args[0] if args else time.strftime("%Y-%m-%d")

        content = await self.diary_store.read(date_str)
        if not content:
            return f"📔 {date_str} 还没有日记哦~"
        return f"📔 {date_str} 的日记：\n\n{content}"

    async def handle_diary_list(self, user_id: str, args: list[str]) -> str:
        """处理 /日记 列表 [年月]"""
        if len(args) >= 2:
            year, month = args[0], args[1].zfill(2)
            dates = await self.diary_store.list_dates(year, month)
            if not dates:
                return f"📔 {year}年{month}月还没有日记~"
            lines = [d["date"] for d in dates]
            return f"📔 {year}年{month}月的日记：\n" + "\n".join(lines)
        else:
            months = await self.diary_store.list_months()
            if not months:
                return "📔 还没有写过日记~"
            lines = [f"{m['year']}年{m['month']}月" for m in months]
            return "📔 有日记的月份：\n" + "\n".join(lines)

    async def handle_memory(self, user_id: str) -> str:
        """处理 /记忆 — 查看画像和统计"""
        persona = await self.persona_store.read(user_id)
        stats = await self.atom_store.get_stats(user_id)

        parts = []
        if persona:
            parts.append(f"🧠 关于你：\n{persona[:500]}")
        parts.append(f"📊 统计：共 {stats['total']} 条记忆")
        if stats.get("by_type"):
            type_labels = {
                "episodic": "事件", "factual": "事实", "preference": "偏好",
                "planned": "计划", "relational": "关系",
            }
            by_type = "\n".join(
                f"  - {type_labels.get(t, t)}: {c}条"
                for t, c in stats["by_type"].items()
            )
            parts.append(by_type)
        return "\n\n".join(parts) if parts else "🧠 还没有关于你的记忆~"

    async def handle_search(self, user_id: str, query: str) -> str:
        """处理 /记忆 搜索 <关键词>"""
        if not query:
            return "💡 请输入关键词，例如：/记忆 搜索 告白"

        result = await self.retriever.get_context_memories(user_id, query, k=5)

        if not result.atoms:
            return f"🔍 没有找到和「{query}」相关的记忆~"

        lines = [f"🔍 「{query}」相关的记忆："]
        for a in result.atoms:
            date = f" ({a.diary_date})" if a.diary_date else ""
            lines.append(f"- [{a.atom_type.value}]{date} {a.content[:200]}")
            lines.append(f"  重要度: {a.importance} | ID: {a.atom_id}")

        return "\n".join(lines)

    async def handle_delete(self, user_id: str, atom_id_str: str) -> str:
        """处理 /记忆 删除 <id>"""
        try:
            atom_id = int(atom_id_str)
        except ValueError:
            return "❌ 请输入有效的记忆 ID"

        success = await self.atom_store.delete(atom_id, user_id)
        if success:
            return f"✅ 已删除记忆 #{atom_id}"
        return "❌ 找不到这条记忆，或你没有权限删除"

    async def handle_rebuild(self, user_id: str, args: list[str]) -> str:
        """处理 /记忆 重构 — 对旧导入数据重新提取原子

        参数：
          /记忆重构        — 只处理原子数 ≤ 1 的日记
          /记忆重构 全部   — 强制重提所有日记（清空旧原子）
        """
        if not self.capturer:
            return "❌ 重构功能不可用（未注入 capturer）"

        from ..utils.diary_helper import parse_diary_content
        import time

        force_all = any(kw in " ".join(args) for kw in ["全部", "force", "all", "full"])

        if force_all:
            rows = await self.diary_store.fetch("""
                SELECT d.id, d.date, d.content
                FROM diary_entries d
                WHERE length(d.content) > 20
                ORDER BY d.id
            """)
        else:
            rows = await self.diary_store.fetch("""
                SELECT d.id, d.date, d.content
                FROM diary_entries d
                WHERE (SELECT COUNT(*) FROM atoms_diary_links l WHERE l.diary_id=d.id) <= 1
                AND length(d.content) > 20
                ORDER BY d.id
            """)

        processed = 0
        skipped = 0
        errors = 0
        messages = []

        if force_all:
            # 先清空所有旧原子
            await self.atom_store.execute("UPDATE memory_atoms SET status='forgotten' WHERE status='active'")
            await self.atom_store.execute("DELETE FROM memory_atoms_fts")
            messages.append("🧹 已清空所有旧原子")
            processed_all = len(rows)
        else:
            processed_all = len(rows)

        for row in rows:
            did = row[0]
            date_str = row[1]
            content = row[2] or ""

            if not force_all:
                atoms = await self.atom_store.fetch(
                    "SELECT a.content FROM memory_atoms a "
                    "JOIN atoms_diary_links l ON a.id = l.atom_id "
                    "WHERE l.diary_id=? AND a.status='active' LIMIT 2",
                    (did,),
                )
                if atoms:
                    atom_text = atoms[0][0] if atoms else ""
                    if atom_text and len(atom_text) < len(content) * 0.8:
                        skipped += 1
                        continue

            try:
                # 剥离 frontmatter，只取正文
                _, body = parse_diary_content(content)
                source_text = body if body else content

                # 调用 LLM 提取原子
                new_atoms = await self.capturer.extract_atoms_for_persona(source_text, user_id)
                if not new_atoms:
                    skipped += 1
                    continue

                # 删除旧原子
                await self.atom_store.execute(
                    "UPDATE memory_atoms SET status='forgotten' WHERE id IN "
                    "(SELECT atom_id FROM atoms_diary_links WHERE diary_id=?)",
                    (did,),
                )

                # 插入新原子（最多 5 条）
                new_atoms = new_atoms[:5]
                for atom in new_atoms:
                    atom.diary_id = did
                    atom.diary_date = date_str
                    atom.prepare_insert()

                ids = await self.atom_store.insert_many(new_atoms)
                for atom, aid in zip(new_atoms, ids):
                    atom.atom_id = aid
                    # 桥表关联
                    try:
                        await self.atom_store.link_atom_to_diary(
                            aid, did, snippet=atom.diary_snippet, importance=atom.importance,
                        )
                    except Exception:
                        pass

                # 更新日记重要度 = 最高原子重要度（精确到日记条目 ID）
                max_imp = max(a.importance for a in new_atoms) if new_atoms else 0.5
                await self.diary_store.update_metadata_by_id(did, importance=max_imp)

                processed += 1
                if processed % 10 == 0:
                    messages.append(f"  已处理 {processed} 条...")

            except Exception as e:
                logger.warning(f"[Memory] 重构日记 #{did} 失败: {e}")
                errors += 1
                await asyncio.sleep(1)

            await asyncio.sleep(0.5)  # LLM 调用间隔

        result = (
            f"✅ 重构完成\n"
            f"处理: {processed} 条\n"
            f"跳过: {skipped} 条（已有原子或无内容）\n"
            f"失败: {errors} 条"
        )
        return result

    async def handle_stats(self, user_id: str) -> str:
        """处理 /记忆 统计"""
        stats = await self.atom_store.get_stats(user_id)
        if stats["total"] == 0:
            return "📊 还没有任何记忆~"

        type_labels = {
            "episodic": "事件", "factual": "事实", "preference": "偏好",
            "planned": "计划", "relational": "关系", "unknown": "未分类",
        }
        by_type = "\n".join(
            f"  - {type_labels.get(t, t)}: {c}条"
            for t, c in stats["by_type"].items()
        )
        return (
            f"📊 记忆统计\n"
            f"总计: {stats['total']} 条\n\n"
            f"按类型分布：\n{by_type}"
        )
