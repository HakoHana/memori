"""共享的 SQLite 存储基类 — 连接池 + 批量操作 + 自动重试 + 锁"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any

import aiosqlite


class BaseDbStore:
    """
    SQLite 存储基类

    特性：
    - 连接池（按 db_path 和 event_loop 缓存，避免反复打开/关闭连接）
    - 自动重试 SQLITE_BUSY（最多 3 次，指数退避）
    - batch_execute / batch_execute_fts 批量操作
    - 统一的 busy_timeout 和 PRAGMA 配置
    - **per-connection asyncio.Lock** 防止共享连接上的并发操作冲突
    """

    _pragmas: list[str] = [
        "PRAGMA journal_mode = WAL",
        "PRAGMA synchronous = NORMAL",
        "PRAGMA cache_size = -8000",
        "PRAGMA busy_timeout = 30000",
    ]
    _busy_timeout_ms: int = 30000
    _max_retries: int = 3
    _retry_delay_base: float = 0.1

    # ── 连接池（类级别）──
    # {db_path: {loop_id: {"conn": ..., "lock": ..., "last_used": ...}}}
    _pool: dict[str, dict[int, dict]] = {}
    _POOL_MAX = 24

    def __init__(self, db_path: str):
        self.db_path = db_path

    # ═══════════════════════════════════════════════════
    #  连接管理（带锁）
    # ═══════════════════════════════════════════════════

    async def _get_conn(self) -> tuple[aiosqlite.Connection, asyncio.Lock]:
        """获取或创建缓存连接及其关联锁"""
        loop = asyncio.get_running_loop()
        lid = id(loop)

        bucket = self._pool.setdefault(self.db_path, {})
        if lid in bucket:
            bucket[lid]["last_used"] = time.monotonic()
            return bucket[lid]["conn"], bucket[lid]["lock"]

        conn = await aiosqlite.connect(self.db_path, timeout=self._busy_timeout_ms / 1000)
        await conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        for p in self._pragmas:
            await conn.execute(p)

        lock = asyncio.Lock()
        bucket[lid] = {"conn": conn, "lock": lock, "last_used": time.monotonic()}

        await self._maybe_evict()
        return conn, lock

    async def _maybe_evict(self):
        """池超限时淘汰最久未用的"""
        total = sum(len(b) for b in self._pool.values())
        if total <= self._POOL_MAX:
            return
        entries = []
        for dp, bucket in self._pool.items():
            for lid, ent in bucket.items():
                entries.append((dp, lid, ent))
        entries.sort(key=lambda x: x[2]["last_used"])
        for dp, lid, ent in entries[: total - self._POOL_MAX]:
            try:
                await ent["conn"].close()
            except Exception:
                pass
            del self._pool[dp][lid]
            if not self._pool[dp]:
                del self._pool[dp]

    async def close(self):
        """关闭当前 event loop 的连接"""
        loop = asyncio.get_running_loop()
        lid = id(loop)
        bucket = self._pool.get(self.db_path, {})
        if lid in bucket:
            try:
                await bucket[lid]["conn"].close()
            except Exception:
                pass
            del bucket[lid]

    @classmethod
    async def close_all(cls):
        """关闭所有连接（异步版本，用于正常关闭流程）"""
        for dp, bucket in list(cls._pool.items()):
            for lid, ent in list(bucket.items()):
                try:
                    await ent["conn"].close()
                except Exception:
                    pass
            del cls._pool[dp]

    @classmethod
    def close_all_sync(cls):
        """关闭所有连接（同步版本，用于解释器关闭阶段）

        当 event loop 已关闭时使用此方法，
        直接关闭底层 sqlite3 连接来释放后台线程。
        """
        for dp, bucket in list(cls._pool.items()):
            for lid, ent in list(bucket.items()):
                try:
                    conn = ent.get("conn")
                    if conn and hasattr(conn, "_connection"):
                        conn._connection.close()
                except Exception:
                    pass
            del cls._pool[dp]

    # ═══════════════════════════════════════════════════
    #  上下文管理器（带锁）
    # ═══════════════════════════════════════════════════

    @asynccontextmanager
    async def _connect(self):
        """
        获取共享连接（自动加锁，防止并发冲突）

        一个 store 实例上的所有 _connect() 调用共享同一个连接，
        通过 asyncio.Lock 序列化访问，避免 WAL 模式下的并发写入冲突。
        """
        conn, lock = await self._get_conn()
        async with lock:
            try:
                yield conn
            except aiosqlite.DatabaseError as e:
                if "locked" in str(e).lower():
                    raise
                raise

    # ═══════════════════════════════════════════════════
    #  快捷方法（自动加锁）
    # ═══════════════════════════════════════════════════

    async def execute(self, sql: str, params: tuple | list | None = None):
        """执行单条 SQL 并提交"""
        async with self._connect() as db:
            c = await db.execute(sql, params or ())
            await db.commit()
            return c

    async def fetch(self, sql: str, params: tuple | list | None = None) -> list:
        """查询返回所有行"""
        async with self._connect() as db:
            return await db.execute_fetchall(sql, params or ())

    async def fetchone(self, sql: str, params: tuple | list | None = None):
        """查询返回第一行"""
        rows = await self.fetch(sql, params)
        return rows[0] if rows else None

    async def commit(self):
        """提交当前连接的事务"""
        async with self._connect() as db:
            await db.commit()

    # ═══════════════════════════════════════════════════
    #  批量操作
    # ═══════════════════════════════════════════════════

    async def batch_execute(self, sql: str, params_list: list[tuple]) -> int:
        """executemany 批量写入（返回影响行数）"""
        if not params_list:
            return 0
        async with self._connect() as db:
            c = await db.executemany(sql, params_list)
            await db.commit()
            return c.rowcount

    async def batch_execute_fts(self, sql: str, params_list: list[tuple]) -> None:
        """FTS 批量插入"""
        if not params_list:
            return
        async with self._connect() as db:
            await db.executemany(sql, params_list)
            await db.commit()

    # ═══════════════════════════════════════════════════
    #  重试执行器
    # ═══════════════════════════════════════════════════

    async def _exec_retry(self, sql: str, params: tuple | list | None = None) -> Any:
        """带自动重试的执行（SQLITE_BUSY 场景）"""
        last_err = None
        for attempt in range(self._max_retries + 1):
            try:
                return await self.execute(sql, params)
            except aiosqlite.DatabaseError as e:
                if "locked" not in str(e).lower():
                    raise
                last_err = e
                if attempt < self._max_retries:
                    await asyncio.sleep(self._retry_delay_base * (2 ** attempt))
        raise last_err  # type: ignore
