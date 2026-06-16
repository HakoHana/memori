"""写操作日志 — 确保多步写入的原子性和可恢复性"""

from __future__ import annotations

import json
import time
from typing import Any

from .base_store import BaseDbStore


class WriteOpLog(BaseDbStore):
    """记录每次写入操作的状态，用于崩溃恢复"""

    async def initialize(self):
        async with self._connect() as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS write_ops (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    op_type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    step TEXT NOT NULL DEFAULT 'started',
                    payload TEXT DEFAULT '{}',
                    error TEXT,
                    retry_count INTEGER DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_write_ops_status
                ON write_ops(status, updated_at)
            """)
            await db.commit()

    async def begin(self, op_type: str, payload: dict = None) -> int:
        """开始一个写操作"""
        now = time.time()
        async with self._connect() as db:
            cursor = await db.execute(
                "INSERT INTO write_ops(op_type, status, step, payload, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (op_type, "pending", "started", json.dumps(payload or {}, ensure_ascii=False), now, now),
            )
            await db.commit()
            return cursor.lastrowid

    async def step(self, op_id: int, step: str):
        """更新操作进度"""
        async with self._connect() as db:
            await db.execute(
                "UPDATE write_ops SET step=?, updated_at=? WHERE id=?",
                (step, time.time(), op_id),
            )
            await db.commit()

    async def complete(self, op_id: int):
        """标记操作完成"""
        async with self._connect() as db:
            await db.execute(
                "UPDATE write_ops SET status='completed', step='done', updated_at=? WHERE id=?",
                (time.time(), op_id),
            )
            await db.commit()

    async def fail(self, op_id: int, error: str):
        """标记操作失败"""
        async with self._connect() as db:
            await db.execute(
                "UPDATE write_ops SET status='failed', error=?, updated_at=? WHERE id=?",
                (error, time.time(), op_id),
            )
            await db.commit()

    async def get_pending(self) -> list[dict]:
        """获取所有未完成的写操作"""
        async with self._connect() as db:
            rows = await db.execute_fetchall(
                "SELECT * FROM write_ops WHERE status='pending' ORDER BY id"
            )
        result = []
        for r in rows:
            result.append({
                "id": r[0], "op_type": r[1], "status": r[2],
                "step": r[3], "payload": json.loads(r[4]) if isinstance(r[4], str) else {},
                "error": r[5], "retry_count": r[6],
            })
        return result

    async def repair_on_startup(self):
        """启动时检查并修复未完成的写操作"""
        pending = await self.get_pending()
        for op in pending:
            step = op["step"]
            op_type = op["op_type"]
            payload = op["payload"]
            if op_type == "capture":
                if step == "diary_written":
                    # 日记已写但原子未写入 → 标记失败，后续重试
                    await self.fail(op["id"], "diary_written but atoms not processed")
                elif step == "started":
                    await self.fail(op["id"], "capture not started (no data written)")
                else:
                    await self.fail(op["id"], "repaired: incomplete on startup")
            else:
                await self.fail(op["id"], "repaired: unknown op_type on startup")
