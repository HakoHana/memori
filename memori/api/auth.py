"""认证与授权 — JWT / API Key 验证 + 社交图谱权限检查

设计原则：
- Dashboard 场景复用 AstrBot 的 JWT（共享 secret）
- 程序调用场景使用独立 API Key
- 权限检查基于社交图谱邻居关系 + weight
- 邻居缓存 1 小时，对话路径零阻塞
"""

from __future__ import annotations

import json
import os
import secrets
import time
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

import logging

logger = logging.getLogger(__name__)


class AuthManager:
    """认证管理器 — 令牌验证 + 邻居缓存 + 权限检查"""

    def __init__(self, graph_engine=None, config: dict | None = None):
        self._graph_engine = graph_engine
        self._config = config or {}
        self._cache: dict[str, tuple[dict[str, float], float]] = {}
        self._cache_ttl = 3600  # 邻居缓存 1 小时
        self._api_keys: dict[str, str] = {}  # {key: user_id}
        self._jwt_secret: str | None = None

        # 从配置加载 API Keys
        self._load_api_keys()

    def set_graph_engine(self, graph_engine):
        self._graph_engine = graph_engine

    def set_jwt_secret(self, secret: str | None):
        self._jwt_secret = secret

    # ── 令牌验证 ────────────────────────────────────────

    async def verify_token(self, token: str | None) -> str | None:
        """验证令牌，返回 user_id

        优先级：
        1. API Key（静态、简单）
        2. JWT（AstrBot dashboard 共享）

        Returns:
            user_id 或 None（无效）
        """
        if not token:
            return None

        # 1. 试 API Key
        uid = self._verify_api_key(token)
        if uid:
            return uid

        # 2. 试 JWT
        uid = await self._verify_jwt(token)
        if uid:
            return uid

        return None

    def _verify_api_key(self, key: str) -> str | None:
        return self._api_keys.get(key)

    async def _verify_jwt(self, token: str) -> str | None:
        if not self._jwt_secret:
            return None
        try:
            import jwt as pyjwt
            payload = pyjwt.decode(token, self._jwt_secret, algorithms=["HS256"])
            username = payload.get("username", "")
            if username:
                return username
        except Exception:
            pass
        return None

    # ── API Key 管理 ────────────────────────────────────

    def generate_api_key(self, user_id: str) -> str:
        """为用户生成新的 API Key"""
        key = f"memori_{secrets.token_hex(24)}"
        self._api_keys[key] = user_id
        self._save_api_keys()
        return key

    def revoke_api_key(self, key: str) -> bool:
        """撤销 API Key"""
        if key in self._api_keys:
            del self._api_keys[key]
            self._save_api_keys()
            return True
        return False

    def list_api_keys(self) -> list[dict]:
        return [{"key": k[-12:]+"...", "user_id": v}
                for k, v in self._api_keys.items()]

    def _load_api_keys(self):
        keys_path = self._config.get("api_keys_path", "")
        if not keys_path:
            return
        try:
            if os.path.exists(keys_path):
                data = json.loads(open(keys_path, encoding="utf-8").read())
                self._api_keys = data.get("keys", {})
        except Exception as e:
            logger.warning(f"[memori] 加载 API Keys 失败: {e}")

    def _save_api_keys(self):
        keys_path = self._config.get("api_keys_path", "")
        if not keys_path:
            return
        try:
            os.makedirs(os.path.dirname(keys_path), exist_ok=True)
            with open(keys_path, "w", encoding="utf-8") as f:
                json.dump({"keys": self._api_keys}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[memori] 保存 API Keys 失败: {e}")

    # ── 邻居缓存 + 权限检查 ─────────────────────────────

    async def get_accessible_users(self, user_id: str) -> dict[str, float]:
        """获取用户可访问的用户列表 {neighbor_id: effective_weight}

        缓存 1 小时，后续请求直接走内存。
        """
        now = time.time()
        cached = self._cache.get(user_id)
        if cached and (now - cached[1]) < self._cache_ttl:
            return cached[0]

        # 从图谱加载
        neighbors: dict[str, float] = {user_id: 1.0}
        if self._graph_engine:
            try:
                neighbors = await self._graph_engine.get_neighbor_ids(user_id)
            except Exception as e:
                logger.warning(f"[memori] 加载邻居失败: {e}")

        self._cache[user_id] = (neighbors, now)
        return neighbors

    def check_access(
        self,
        neighbors: dict[str, float],
        target_uid: str,
        min_weight: float = 0.0,
    ) -> bool:
        """检查是否有权限访问目标用户

        Args:
            neighbors: get_accessible_users() 的返回值
            target_uid: 要访问的用户 ID
            min_weight: 所需最低权重

        Returns:
            True 有权限 / False 无权限
        """
        return neighbors.get(target_uid, 0) >= min_weight

    def invalidate_cache(self, user_id: str):
        """使指定用户的缓存失效"""
        self._cache.pop(user_id, None)

    # ── HTTP 中间件 ─────────────────────────────────────

    async def auth_middleware(self, request: Request, call_next):
        """FastAPI 中间件 — 自动验证请求

        公开路径（免认证）：
        - /health, /docs, /openapi.json, /redoc
        - /webui/**（前端静态文件）
        """
        path = request.url.path

        # 公开路径放行
        if self._is_public_path(path):
            return await call_next(request)

        # 如果 auth_disabled=True，跳过所有校验（开发/单机场景）
        if self._config.get("auth_disabled", True):
            request.state.user_id = "local_dev"
            return await call_next(request)

        # 提取 token
        token = self._extract_token(request)
        user_id = await self.verify_token(token) if token else None

        if not user_id:
            return JSONResponse(
                status_code=401,
                content={"ok": False, "error": "未授权，请提供有效令牌"},
            )

        # 注入当前用户
        request.state.user_id = user_id
        return await call_next(request)

    # ── 辅助 ────────────────────────────────────────────

    @staticmethod
    def _is_public_path(path: str) -> bool:
        public_prefixes = (
            "/health", "/docs", "/openapi.json", "/redoc",
            "/webui", "/settings", "/config",
        )
        return path.startswith(public_prefixes)

    @staticmethod
    def _extract_token(request: Request) -> str | None:
        """从请求中提取 token

        优先级：Authorization header → cookie → query param
        """
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:]
        # 从 cookie 读取（AstrBot dashboard 的 JWT cookie）
        cookie = request.cookies.get("astrbot_dashboard_jwt")
        if cookie:
            return cookie
        token = request.query_params.get("token")
        return token or None

    @staticmethod
    async def get_current_user(request: Request) -> str:
        """FastAPI 依赖 — 获取当前用户 ID"""
        uid = getattr(request.state, "user_id", None)
        if uid is None:
            raise HTTPException(status_code=401, detail="未授权")
        return uid
