"""共享的 SQLite 存储基类"""

from __future__ import annotations

from contextlib import asynccontextmanager

import aiosqlite


class BaseDbStore:
    """SQLite 存储基类，提供共享的 _connect 和 _db_path"""

    # 子类可覆写此列表自定义 PRAGMA
    _pragmas: list[str] = [
        "PRAGMA journal_mode = WAL",
    ]
    _busy_timeout_ms: int = 5000

    def __init__(self, db_path: str):
        self.db_path = db_path

    @asynccontextmanager
    async def _connect(self):
        db = await aiosqlite.connect(self.db_path)
        try:
            await db.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            for pragma in self._pragmas:
                await db.execute(pragma)
            yield db
        finally:
            await db.close()

    async def execute(self, sql: str, params=None):
        """快捷执行单条 SQL（用于不需要返回值的操作）"""
        async with self._connect() as db:
            cursor = await db.execute(sql, params or ())
            await db.commit()
            return cursor

    async def fetch(self, sql: str, params=None):
        """快捷查询返回所有行"""
        async with self._connect() as db:
            return await db.execute_fetchall(sql, params or ())
