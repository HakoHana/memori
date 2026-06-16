"""内置 EmbeddingProvider 实现 — 三种部署方式

1. LocalEmbeddingProvider  — 本地 sentence-transformers（需 pip install memori[embedding]）
2. OllamaEmbeddingProvider — 通过 Ollama API 调用（零额外依赖）
3. RemoteEmbeddingProvider — OpenAI 兼容远程 API（零额外依赖）

用法（由 MemoryCore 根据配置自动选择，无需手动实例化）:
    embed_id = "local"                    → LocalEmbeddingProvider
    embed_id = "my-ollama" (type=ollama)  → OllamaEmbeddingProvider
    embed_id = "my-api"   (type=api)      → RemoteEmbeddingProvider
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx

from .adapters import EmbeddingProvider

logger = logging.getLogger("memori")


# ═══════════════════════════════════════════════════════════
#  本地 sentence-transformers（调包）
# ═══════════════════════════════════════════════════════════


class LocalEmbeddingProvider(EmbeddingProvider):
    """基于 sentence-transformers 的本地嵌入模型（懒加载 + 异步初始化）

    模型在首次调用 embed() 时自动异步加载，
    不阻塞构造函数与事件循环，适合在 FastAPI 等异步场景中使用。

    需要安装: pip install 'memori[embedding]'
    """

    def __init__(self, model_name: str = "BAAI/bge-m3"):
        self._model_name = model_name
        self._model = None
        self._dim = None
        self._loaded = False
        self._load_error: str | None = None
        self._last_retry_time: float = 0
        self._loading_task: asyncio.Task | None = None

    async def _load_model(self):
        """实际加载逻辑（保证只执行一次，由 ensure_loaded / preload 触发）"""
        # 再次检查——可能在等待 _loading_task 时已经加载完了
        if self._loaded:
            return

        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            raise ImportError(
                "LocalEmbeddingProvider 需要 sentence-transformers。\n"
                "请运行: pip install 'memori[embedding]'"
            )

        loop = asyncio.get_event_loop()

        # 第一步：只读本地缓存，零网络
        first_err = None
        try:
            self._model = await loop.run_in_executor(
                None,
                lambda: SentenceTransformer(self._model_name, local_files_only=True),
            )
        except Exception as e:
            first_err = e

        # 第二步：缓存未命中，允许联网下载
        if first_err is not None:
            try:
                self._model = await loop.run_in_executor(
                    None,
                    lambda: SentenceTransformer(self._model_name),
                )
            except Exception as e:
                self._load_error = str(e)
                self._last_retry_time = time.time()
                self._model = None
                raise RuntimeError(
                    f"嵌入模型加载失败（本地缓存 + 联网均不可用）: {e}"
                ) from e

        try:
            self._dim = self._model.get_embedding_dimension()
            self._loaded = True
            self._load_error = None
        except Exception as e:
            self._model = None
            self._load_error = str(e)
            self._last_retry_time = time.time()
            raise

    async def ensure_loaded(self):
        """确保模型已加载——如有进行中的加载则等待，否则启动加载

        支持并发调用：多个协程同时调用时，只会启动一次实际加载。
        """
        if self._loaded:
            return

        # 有进行中的加载 → 等待它完成
        if self._loading_task is not None and not self._loading_task.done():
            await self._loading_task
            return

        # 冷却期内不再重试
        if self._load_error and (time.time() - self._last_retry_time) < 60:
            raise RuntimeError(
                f"嵌入模型上次加载失败（60 秒冷却期）: {self._load_error}"
            )

        # 启动新的加载任务（确保 ensure_loaded 可重入）
        self._loading_task = asyncio.ensure_future(self._load_model())
        try:
            await self._loading_task
        finally:
            self._loading_task = None

    def preload(self):
        """异步预加载——启动后台加载，不等待完成

        适合在 MemoryCore 初始化完成后调用，让模型在后台加载，
        等第一次 capture 需要 embed 时模型已就绪。
        """
        if self._loaded or self._loading_task is not None:
            return
        task = asyncio.ensure_future(self._load_model())
        # 消费异常避免 "Task exception was never retrieved" 警告
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        self._loading_task = task

    async def embed(self, text: str) -> list[float]:
        await self.ensure_loaded()
        return self._model.encode(text).tolist()

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        await self.ensure_loaded()
        return self._model.encode(texts).tolist()

    @property
    def dimension(self) -> int:
        if self._dim is None:
            return 0
        return self._dim

    @property
    def model_name(self) -> str:
        return self._model_name

    async def close(self):
        """释放模型资源"""
        if self._loading_task is not None and not self._loading_task.done():
            self._loading_task.cancel()
            try:
                await self._loading_task
            except (asyncio.CancelledError, Exception):
                pass
            self._loading_task = None
        self._model = None
        self._dim = None
        self._loaded = False
        self._load_error = None
        self._last_retry_time = 0
        logger.info("[LocalEmbedding] 模型资源已释放")


# ═══════════════════════════════════════════════════════════
#  Ollama API（零额外依赖）
# ═══════════════════════════════════════════════════════════


class OllamaEmbeddingProvider(EmbeddingProvider):
    """通过 Ollama API 调用嵌入模型

    需要本地运行 Ollama 服务（默认 http://localhost:11434），
    并已 pull 了对应模型（如 bge-m3、nomic-embed-text 等）。

    API 文档: https://github.com/ollama/ollama/blob/main/docs/api.md
    """

    def __init__(self, api_base: str, model: str):
        self._api_base = api_base.rstrip("/")
        self._model = model
        self._dim = None
        self._client = httpx.AsyncClient(timeout=60)

    async def _ensure_dim(self):
        """发一条短文本探测向量维度"""
        if self._dim is not None:
            return
        try:
            resp = await self._client.post(
                f"{self._api_base}/api/embeddings",
                json={"model": self._model, "prompt": "."},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            emb = data.get("embedding", [])
            self._dim = len(emb) if emb else 0
        except Exception as e:
            logger.warning(f"[OllamaEmbedding] 维度探测失败: {e}")
            self._dim = 0

    async def embed(self, text: str) -> list[float]:
        await self._ensure_dim()
        resp = await self._client.post(
            f"{self._api_base}/api/embeddings",
            json={"model": self._model, "prompt": text},
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("embedding", [])

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # Ollama API 不支持批量，逐条调用
        results = []
        for t in texts:
            results.append(await self.embed(t))
        return results

    async def close(self):
        """释放持久 httpx 客户端"""
        try:
            await self._client.aclose()
        except Exception as e:
            logger.warning(f"[OllamaEmbedding] 关闭客户端异常: {e}")

    @property
    def dimension(self) -> int:
        if self._dim is None:
            return 0
        return self._dim

    @property
    def model_name(self) -> str:
        return self._model


# ═══════════════════════════════════════════════════════════
#  远程 API（OpenAI 兼容，零额外依赖）
# ═══════════════════════════════════════════════════════════


class RemoteEmbeddingProvider(EmbeddingProvider):
    """OpenAI 兼容的远程嵌入 API

    支持所有兼容 OpenAI Embedding API 的服务:
      - OpenAI:         https://api.openai.com/v1
      - Azure OpenAI:   https://<name>.openai.azure.com
      - vLLM / LocalAI: http://localhost:8000/v1
      - 各种代理/中转服务

    请求格式: POST {api_base}/embeddings
              Authorization: Bearer {api_key}
              {"model": "{model}", "input": "{text}"}
    """

    def __init__(self, api_base: str, api_key: str, model: str):
        self._api_base = api_base.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._dim = None
        self._client = httpx.AsyncClient(timeout=120)

    async def _ensure_dim(self):
        """发一条短文本探测向量维度"""
        if self._dim is not None:
            return
        try:
            resp = await self._client.post(
                f"{self._api_base}/embeddings",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"model": self._model, "input": "."},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            emb = data.get("data", [{}])[0].get("embedding", [])
            self._dim = len(emb) if emb else 0
        except Exception as e:
            logger.warning(f"[RemoteEmbedding] 维度探测失败: {e}")
            self._dim = 0

    async def embed(self, text: str) -> list[float]:
        await self._ensure_dim()
        resp = await self._client.post(
            f"{self._api_base}/embeddings",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={"model": self._model, "input": text},
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", [{}])[0].get("embedding", [])

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = await self._client.post(
            f"{self._api_base}/embeddings",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={"model": self._model, "input": texts},
        )
        resp.raise_for_status()
        data = resp.json()
        # 按输入顺序排列
        results = [{}] * len(texts)
        for item in data.get("data", []):
            idx = item.get("index", 0)
            if 0 <= idx < len(texts):
                results[idx] = item.get("embedding", [])
        return results

    async def close(self):
        """释放持久 httpx 客户端"""
        try:
            await self._client.aclose()
        except Exception as e:
            logger.warning(f"[RemoteEmbedding] 关闭客户端异常: {e}")

    @property
    def dimension(self) -> int:
        if self._dim is None:
            return 0
        return self._dim

    @property
    def model_name(self) -> str:
        return self._model


# ═══════════════════════════════════════════════════════════
#  工厂函数
# ═══════════════════════════════════════════════════════════


def create_embed_provider(
    embed_id: str,
    providers: list[dict],
    embed_model_name: str = "BAAI/bge-m3",
) -> EmbeddingProvider | None:
    """根据 embed_provider_id 和 _providers 列表创建对应的 EmbeddingProvider

    Args:
        embed_id:        配置中的 embed_provider_id
        providers:       _providers 列表（来自配置）
        embed_model_name: 仅 local 类型时有效

    Returns:
        EmbeddingProvider 实例，或 None（未找到/类型不匹配）
    """
    if not embed_id:
        return None

    # 特殊值 "local" = 本地 sentence-transformers
    if embed_id == "local":
        return LocalEmbeddingProvider(embed_model_name)

    # 在 providers 列表中查找
    for p in providers:
        if p.get("name") != embed_id:
            continue
        ptype = p.get("type", "")

        if ptype == "embed:local":
            model = p.get("model", embed_model_name)
            return LocalEmbeddingProvider(model)

        if ptype == "embed:ollama":
            return OllamaEmbeddingProvider(
                api_base=p.get("api_base", ""),
                model=p.get("model", "bge-m3"),
            )

        if ptype in ("embed:api", "embed:remote"):
            return RemoteEmbeddingProvider(
                api_base=p.get("api_base", ""),
                api_key=p.get("api_key", ""),
                model=p.get("model", "text-embedding-3-small"),
            )

        # 旧的 provider 没有 type → 默认当远程 API（向后兼容）
        logger.warning(
            "[embed] 提供商 %r 未设置 type，默认当作远程 API 使用；"
            "建议在提供商配置中添加 type=\"embed:api\"",
            embed_id,
        )
        return RemoteEmbeddingProvider(
            api_base=p.get("api_base", ""),
            api_key=p.get("api_key", ""),
            model=p.get("model", "text-embedding-3-small"),
        )

    logger.warning("[embed] 未找到提供商: %s", embed_id)
    return None
