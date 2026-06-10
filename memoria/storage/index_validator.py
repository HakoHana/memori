"""索引一致性检查 — 启动时检测数据完整性并自动修复"""

from __future__ import annotations

from .base_store import BaseDbStore


class IndexValidator(BaseDbStore):
    """检查各表与索引的一致性"""

    async def validate_all(self) -> dict[str, dict]:
        """运行全部检查，返回每项的检查结果"""
        results = {}

        results["atoms_fts"] = await self._check_atoms_fts()
        results["atom_ids"] = await self._check_atom_ids()
        results["graph_integrity"] = await self._check_graph_integrity()
        results["orphan_atoms"] = await self._check_orphan_atoms()

        results["summary"] = {
            "all_passed": all(r.get("passed", False) for r in results.values() if isinstance(r, dict)),
            "total_issues": sum(len(r.get("issues", [])) for r in results.values() if isinstance(r, dict)),
        }

        return results

    async def _check_atoms_fts(self) -> dict:
        """检查 memory_atoms 与 FTS5 索引是否一致"""
        result = {"name": "原子FTS索引", "passed": True, "issues": []}
        async with self._connect() as db:
            atoms_count = (await db.execute_fetchall("SELECT COUNT(*) FROM memory_atoms"))[0][0]
            fts_count = (await db.execute_fetchall("SELECT COUNT(*) FROM memory_atoms_fts"))[0][0]

        if atoms_count != fts_count:
            result["passed"] = False
            result["issues"].append(f"原子表 {atoms_count} 行 vs FTS {fts_count} 行")
            # 自动修复
            await self._rebuild_fts()
            result["fixed"] = True
        return result

    async def _check_atom_ids(self) -> dict:
        """检查原子的 ID 连续性"""
        result = {"name": "原子ID", "passed": True, "issues": []}
        async with self._connect() as db:
            max_id = (await db.execute_fetchall("SELECT MAX(id) FROM memory_atoms"))[0][0]
            total = (await db.execute_fetchall("SELECT COUNT(*) FROM memory_atoms"))[0][0]
        if max_id and total:
            expected_max = total
            if max_id > expected_max * 1.5:
                result["issues"].append(f"ID 不连续: max={max_id}, count={total}")
        return result

    async def _check_graph_integrity(self) -> dict:
        """检查图边的外键完整性（source 和 target 双向检查）并自动修复"""
        result = {"name": "图谱完整性", "passed": True, "issues": []}
        async with self._connect() as db:
            # 检查 source_node_id 悬挂
            bad_source = await db.execute_fetchall("""
                SELECT COUNT(*) FROM graph_edges e
                LEFT JOIN graph_nodes n ON e.source_node_id = n.id
                WHERE n.id IS NULL
            """)
            # 检查 target_node_id 悬挂
            bad_target = await db.execute_fetchall("""
                SELECT COUNT(*) FROM graph_edges e
                LEFT JOIN graph_nodes n ON e.target_node_id = n.id
                WHERE n.id IS NULL
            """)

        total_bad = (bad_source[0][0] if bad_source else 0) + (bad_target[0][0] if bad_target else 0)
        if total_bad > 0:
            result["passed"] = False
            result["issues"].append(f"存在 {total_bad} 条边指向不存在的节点")
            # 自动修复：删除所有悬挂边
            await self._fix_dangling_edges()
            result["fixed"] = True
            result["issues"].append(f"已自动清理 {total_bad} 条悬挂边")
        return result

    async def _fix_dangling_edges(self):
        """删除 source_node_id 或 target_node_id 悬挂的边"""
        async with self._connect() as db:
            await db.execute("""
                DELETE FROM graph_edges WHERE id IN (
                    SELECT e.id FROM graph_edges e
                    LEFT JOIN graph_nodes n ON e.source_node_id = n.id
                    WHERE n.id IS NULL
                )
            """)
            await db.execute("""
                DELETE FROM graph_edges WHERE id IN (
                    SELECT e.id FROM graph_edges e
                    LEFT JOIN graph_nodes n ON e.target_node_id = n.id
                    WHERE n.id IS NULL
                )
            """)
            await db.commit()

    async def _check_orphan_atoms(self) -> dict:
        """检查没有关联日记的原子"""
        result = {"name": "孤立原子", "passed": True, "issues": []}
        async with self._connect() as db:
            orphans = await db.execute_fetchall("""
                SELECT COUNT(*) FROM memory_atoms a
                WHERE a.diary_date NOT IN (SELECT date FROM diary_entries)
            """)
        if orphans and orphans[0][0] > 0:
            result["issues"].append(f"未关联日记的原子: {orphans[0][0]} 条")
        return result

    async def _rebuild_fts(self):
        """重建 FTS5 索引"""
        async with self._connect() as db:
            await db.execute("DELETE FROM memory_atoms_fts")
            atoms = await db.execute_fetchall(
                "SELECT id, content, user_id FROM memory_atoms"
            )
            for a in atoms:
                await db.execute(
                    "INSERT INTO memory_atoms_fts(atom_id, content, user_id) VALUES (?,?,?)",
                    (a[0], a[1], a[2]),
                )
            await db.commit()
