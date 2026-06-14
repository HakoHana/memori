"""memori 内部模块接口定义 — 依赖倒置原则（DIP）

所有核心模块通过此处的抽象接口解耦，而非直接依赖具体实现。
配合依赖注入，让高层模块（MemoryCore）不依赖低层细节。

用法:
    from .interfaces import ICapturer, IRetriever, ...
    class MyCapturer(ICapturer): ...
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable

from ..models.memory_atom import (
    CaptureJudgeResult,
    CaptureResult,
    MemoryAtom,
    RecallResult,
)


# ═══════════════════════════════════════════════════════════
#  抓取器
# ═══════════════════════════════════════════════════════════

class ICapturer(ABC):
    """从对话中提取记忆的抓取器接口"""

    @abstractmethod
    async def capture(
        self, user_id: str, conversation_summary: str, judge_result: CaptureJudgeResult
    ) -> CaptureResult:
        """完整抓取流水线：Judge → 日记 → 原子（合并模式或分步）"""
        ...

    @abstractmethod
    async def extract_atoms_for_persona(
        self, diary_content: str, user_id: str
    ) -> list[MemoryAtom]:
        """为画像更新提取原子"""
        ...

    @abstractmethod
    async def should_capture(self, conversation_summary: str) -> CaptureJudgeResult:
        """判断对话是否值得记录"""
        ...


# ═══════════════════════════════════════════════════════════
#  检索引擎
# ═══════════════════════════════════════════════════════════

class IRetriever(ABC):
    """记忆检索引擎接口"""

    @abstractmethod
    async def get_context_memories(
        self, user_id: str, query: str, k: int | None = None
    ) -> RecallResult:
        """生成供注入用的记忆文本（含格式化 + 画像）"""
        ...

    @abstractmethod
    async def recall(
        self, user_id: str, query: str, k: int | None = None
    ) -> list[MemoryAtom]:
        """召回相关记忆（双路检索 + RRF 融合）"""
        ...

    @abstractmethod
    async def get_recent_context(
        self, user_id: str, session_id: str = "", limit: int = 20, bot_name: str = "我"
    ) -> str:
        """获取最近对话上下文"""
        ...

    @abstractmethod
    async def search_diaries(self, query: str, k: int = 5) -> list[dict]:
        """搜索日记全文（全库，不按用户过滤）"""
        ...

    @abstractmethod
    async def hybrid_search(self, user_id: str, query: str, k: int = 5) -> dict:
        """混合搜索：原子 + 日记"""
        ...


# ═══════════════════════════════════════════════════════════
#  画像引擎
# ═══════════════════════════════════════════════════════════

class IPersonaEngine(ABC):
    """用户画像引擎接口"""

    @abstractmethod
    async def get_persona(self, uid: str) -> str | None:
        """获取用户画像摘要（带缓存）"""
        ...

    @abstractmethod
    async def incremental_update(
        self,
        uid: str,
        new_diaries: list[str] | None = None,
        new_facts: list[str] | None = None,
    ) -> bool:
        """增量更新画像"""
        ...

    @abstractmethod
    async def full_rebuild(self, uid: str, days: int = 90) -> str | None:
        """全量重建画像"""
        ...

    @abstractmethod
    async def invalidate_cache(self, uid: str):
        """清除用户画像缓存"""
        ...


# ═══════════════════════════════════════════════════════════
#  图谱引擎
# ═══════════════════════════════════════════════════════════

class IGraphEngine(ABC):
    """图谱引擎接口

    职责：实体关系记忆，不取代原子关键词/向量检索。
    写入：index_diary（单一入口，被 Capturer 回调）
    读取：query_neighbors / find_linked_diaries
    """

    @abstractmethod
    async def index_diary(
        self,
        diary_id: int,
        content: str,
        entities: list[str] | None = None,
    ):
        """从日记 content 建立图谱索引"""
        ...

    @abstractmethod
    async def query_neighbors(self, entity_name: str) -> dict:
        """查询实体的邻居，返回子图"""
        ...

    @abstractmethod
    async def find_linked_diaries(self, node_ids: list[int]) -> list[int]:
        """从实体节点沿 mention 边找到关联的 diary_id"""
        ...

    @abstractmethod
    async def batch_cooccur(self) -> int:
        """增量统计 co_occur 边权重"""
        ...


# ═══════════════════════════════════════════════════════════
#  指令处理器
# ═══════════════════════════════════════════════════════════

class ICommandHandler(ABC):
    """用户指令处理接口"""

    @abstractmethod
    async def handle_diary(self, user_id: str, args: list[str]) -> str:
        ...

    @abstractmethod
    async def handle_diary_list(self, user_id: str, args: list[str]) -> str:
        ...

    @abstractmethod
    async def handle_memory(self, user_id: str) -> str:
        ...

    @abstractmethod
    async def handle_search(self, user_id: str, query: str) -> str:
        ...

    @abstractmethod
    async def handle_delete(self, user_id: str, args: list[str]) -> str:
        ...

    @abstractmethod
    async def handle_stats(self, user_id: str) -> str:
        ...

    @abstractmethod
    async def handle_rebuild(self, user_id: str, args: list[str]) -> str:
        ...


# ═══════════════════════════════════════════════════════════
#  记忆注入器
# ═══════════════════════════════════════════════════════════

class IMemoryInjector(ABC):
    """记忆注入器接口"""

    @abstractmethod
    def inject(
        self,
        memory_text: str,
        persona_text: str | None,
        system_prompt: str,
        user_message: str,
        user_name: str = "",
    ) -> tuple[str, str]:
        """将记忆注入到提示词的指定位置

        Returns:
            (modified_system_prompt, modified_user_message)
        """
        ...

    @abstractmethod
    def reload_config(self, config: dict[str, Any]):
        """热加载配置"""
        ...


# ═══════════════════════════════════════════════════════════
#  后台暖处理队列
# ═══════════════════════════════════════════════════════════

class IWarmProcessor(ABC):
    """后台异步任务队列接口"""

    @abstractmethod
    async def enqueue(
        self,
        user_id: str,
        conversation_text: str,
        state,
        sender_name: str = "",
        on_done: Callable | None = None,
    ):
        """将一次整理任务加入后台队列"""
        ...

    @abstractmethod
    async def start(self):
        """启动队列消费者"""
        ...

    @abstractmethod
    async def stop(self):
        """停止队列消费者"""
        ...


# ═══════════════════════════════════════════════════════════
#  调度器 + 会话状态管理器
# ═══════════════════════════════════════════════════════════

class IConsolidationManager(ABC):
    """调度器接口"""

    @abstractmethod
    async def initialize(self):
        """从数据库恢复会话状态"""
        ...

    @abstractmethod
    async def on_message(
        self, user_id: str, conversation_text: str, sender_name: str = ""
    ):
        """消息入口：仅更新活动时间，不计数"""
        ...

    @abstractmethod
    async def on_round_complete(self, user_id: str, session_id: str = ""):
        """Bot 完成一轮对话后调用：累计轮数 → 达阈值触发整理"""
        ...

    @abstractmethod
    async def destroy(self):
        """销毁调度器"""
        ...

    @abstractmethod
    def update_config(self, config: dict[str, Any]):
        """热更新配置（避免外部直接写内部属性）"""
        ...

    @abstractmethod
    def set_warm_processor(self, warm_processor: IWarmProcessor):
        """注入 WarmProcessor（初始化顺序解耦）"""
        ...

    @abstractmethod
    def get_state(self, user_id: str):
        """获取用户会话状态"""
        ...


# ═══════════════════════════════════════════════════════════
#  用户身份解析器
# ═══════════════════════════════════════════════════════════

class IUserIdentityResolver(ABC):
    """用户身份解析器 — GraphEngine 通过此接口获取用户信息

    将图谱引擎与 user_identities / user_persona 表解耦。
    """

    @abstractmethod
    async def resolve_display_name(self, display_name: str) -> str | None:
        """display_name → 系统 uid，没有返回 None"""
        ...

    @abstractmethod
    async def get_persona_summary(self, uid: str) -> str:
        """获取用户画像摘要"""
        ...

    @abstractmethod
    async def get_persona_full(self, display_name: str) -> dict | None:
        """从 display_name 获取完整画像（含 tags/tier/name）"""
        ...
