"""归档管理器 — 冷存储 + Markdown 导出"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from .logger import logger
from ..storage.diary_store import DiaryStore
from ..core.diary_helper import parse_diary_content, build_diary_content


class Archiver:
    """冷存储归档管理器

    功能：
    - 每日扫描 diary_entries → 符合条件的导出 Markdown
    - 单篇日记导出
    - 从冷存储恢复
    """

    def __init__(
        self,
        diary_store: DiaryStore,
        archive_dir: str = "./memory_archive",
        config: dict[str, Any] | None = None,
    ):
        self.diary_store = diary_store
        self.archive_dir = Path(archive_dir)
        self.config = config or {}
        self.warm_days = self.config.get("warm_days", 90)
        self.cold_importance = self.config.get("cold_importance_threshold", 0.1)
        self.max_summary_chars = self.config.get("max_summary_chars", 200)

    async def archive_daily(self, dry_run: bool = False) -> int:
        """扫描 diary_entries，符合条件的导出 Markdown

        条件：timestamp > warm_days AND importance < cold_importance
        """
        import time
        cutoff = time.time() - self.warm_days * 86400
        rows = await self.diary_store.fetch("""
            SELECT id, user_id, date, content, importance, created_at
            FROM diary_entries
            WHERE created_at < ? AND importance < ? AND archived = 0
            ORDER BY user_id, date
        """, (cutoff, self.cold_importance))

        if not rows:
            return 0

        archived = 0
        for r in rows:
            did, uid, date_str, content, imp, ts = r
            if dry_run:
                logger.info(f"[Archive] [DRY] #{did} {date_str} imp={imp}")
                archived += 1
                continue

            try:
                # 解析 frontmatter
                fm, body = parse_diary_content(content or "")
                summary = (body or content or "")[:self.max_summary_chars]

                # 写入冷存储
                file_path = await self._write_cold(uid, date_str, content or "", did)

                # 更新 diary_entries
                from ..storage.diary_store import DiaryStore
                await self.diary_store.execute("""
                    UPDATE diary_entries
                    SET content = ?, archived = 1, updated_at = ?
                    WHERE id = ?
                """, (summary, time.time(), did))

                archived += 1
            except Exception as e:
                logger.warning(f"[Archive] 归档日记 #{did} 失败: {e}")

        return archived

    async def export_diary(self, diary_id: int) -> str | None:
        """单篇日记导出为 Markdown（返回文件路径）"""
        row = await self.diary_store.fetchone(
            "SELECT id, user_id, date, content FROM diary_entries WHERE id=?",
            (diary_id,),
        )
        if not row:
            return None
        return await self._write_cold(row[1], row[2], row[3], row[0])

    async def _write_cold(self, uid: str, date_str: str, content: str,
                           diary_id: int) -> str:
        """写入冷存储文件"""
        date_part = date_str.replace("-", "")[:6] if date_str else "unknown"
        day = date_str[-2:] if date_str else "00"
        ym = date_str[:7] if date_str else "unknown"
        year = date_str[:4] if date_str else "unknown"
        month = date_str[5:7] if date_str else "00"

        dir_path = self.archive_dir / f"u_{uid}" / year / month
        dir_path.mkdir(parents=True, exist_ok=True)
        file_path = dir_path / f"{date_str}.md"

        # 同天多条日记：检查文件是否存在，追加
        header = f"---\nuid: \"{uid}\"\ndate: {date_str}\narchived_at: {time.strftime('%Y-%m-%d %H:%M:%S')}\n---\n\n"
        entry = f"## entry {diary_id}\nid: {diary_id}\ntimestamp: {time.time()}\n---\n{content.strip()}\n\n"

        mode = "a" if file_path.exists() else "w"
        with open(file_path, mode, encoding="utf-8") as f:
            if mode == "w":
                f.write(header)
            f.write(entry)

        logger.info(f"[Archive] 已导出: {file_path}")
        return str(file_path)

    async def restore_from_archive(self, diary_id: int) -> str | None:
        """从冷存储恢复原日记内容"""
        row = await self.diary_store.fetchone(
            "SELECT user_id, date FROM diary_entries WHERE id=? AND archived=1",
            (diary_id,),
        )
        if not row:
            return None
        uid, date_str = row
        year = date_str[:4]
        month = date_str[5:7]
        file_path = self.archive_dir / f"u_{uid}" / year / month / f"{date_str}.md"
        if not file_path.exists():
            return None

        with open(file_path, encoding="utf-8") as f:
            content = f.read()

        # 查找该日记的 entry
        marker = f"## entry {diary_id}"
        parts = content.split(marker, 1)
        if len(parts) < 2:
            return content  # 整文件返回

        entry_text = parts[1].split("## entry")[0].strip()
        # 去掉第一行的 timestamp 和 ---
        lines = entry_text.split("\n")
        body_lines = []
        in_header = False
        for line in lines:
            if line.strip() == "---":
                in_header = not in_header
                continue
            if not in_header:
                body_lines.append(line)

        return "\n".join(body_lines).strip()
