"""记忆注入器 — 控制记忆放到提示词的什么位置"""

from __future__ import annotations

from typing import Any

from .interfaces import IMemoryInjector


class MemoryInjector(IMemoryInjector):
    """
    记忆注入器

    用户可在 WebUI 配置：
    - 注入位置（系统提示词末尾 / 用户消息前 / 用户消息后 / 知识库 / 不注入）
    - 自定义模板（{{content}} 代表记忆内容，{{user}} 代表用户名）
    - 是否用标签包裹
    """

    POSITION_SYSTEM_PROMPT_SUFFIX = "system_prompt_suffix"
    POSITION_USER_MESSAGE_PREFIX = "user_message_prefix"
    POSITION_USER_MESSAGE_SUFFIX = "user_message_suffix"
    POSITION_KNOWLEDGE_SECTION = "knowledge_section"
    POSITION_MANUAL_ONLY = "manual_only"

    POSITION_LABELS = {
        POSITION_SYSTEM_PROMPT_SUFFIX: "系统提示词末尾",
        POSITION_USER_MESSAGE_PREFIX: "用户消息之前",
        POSITION_USER_MESSAGE_SUFFIX: "用户消息之后",
        POSITION_KNOWLEDGE_SECTION: "知识库区域",
        POSITION_MANUAL_ONLY: "不注入，仅手动调用",
    }

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}
        self.position = self.config.get("injection_position", self.POSITION_SYSTEM_PROMPT_SUFFIX)
        self.template = self.config.get("injection_template", "")
        self.use_tag = self.config.get("injection_use_tag", True)

    def reload_config(self, config: dict[str, Any]):
        """热加载配置"""
        self.position = config.get("injection_position", self.POSITION_SYSTEM_PROMPT_SUFFIX)
        self.template = config.get("injection_template", "")
        self.use_tag = config.get("injection_use_tag", True)

    def format_memory_block(self, memory_text: str, user_name: str = "") -> str:
        """格式化记忆文本块"""
        if not memory_text or not memory_text.strip():
            return ""

        # 模板替换
        if self.template:
            block = self.template.replace("{{content}}", memory_text)
            block = block.replace("{{user}}", user_name)
        else:
            block = (
                "【📖 这是我的长期记忆，来自我亲身经历或用户明确告诉我的信息，是真实可靠的，不需要额外验证。】\n"
                f"{memory_text}"
            )

        # 标签包裹
        if self.use_tag:
            block = f"<memory>\n{block}\n</memory>"

        return block

    def inject(
        self,
        memory_text: str,
        persona_text: str | None,
        system_prompt: str,
        user_message: str,
        user_name: str = "",
    ) -> tuple[str, str]:
        """
        注入记忆到指定位置

        返回 (modified_system_prompt, modified_user_message)
        """
        if self.position == self.POSITION_MANUAL_ONLY:
            return system_prompt, user_message

        # 合并画像 + 记忆
        combined = ""
        if persona_text:
            combined += f"关于你：\n{persona_text[:300]}\n\n"
        if memory_text:
            combined += memory_text

        block = self.format_memory_block(combined, user_name)
        if not block:
            return system_prompt, user_message

        if self.position == self.POSITION_SYSTEM_PROMPT_SUFFIX:
            sep = "\n\n" if system_prompt and not system_prompt.endswith("\n") else ""
            return f"{system_prompt}{sep}{block}", user_message

        elif self.position == self.POSITION_USER_MESSAGE_PREFIX:
            return system_prompt, f"{block}\n\n{user_message}"

        elif self.position == self.POSITION_USER_MESSAGE_SUFFIX:
            return system_prompt, f"{user_message}\n\n{block}"

        elif self.position == self.POSITION_KNOWLEDGE_SECTION:
            # 在系统提示词中标记为知识库部分
            sep = "\n\n" if system_prompt and not system_prompt.endswith("\n") else ""
            return (
                f"{system_prompt}{sep}[知识库]\n{block}\n[/知识库]",
                user_message,
            )

        return system_prompt, user_message
