"""Capture 流水线步骤 — 可插拔的策略链

替代 capturer.py 中硬编码的 feature flags（如 _enable_rule_classifier）。
新增步骤只需新建 CaptureStep 子类并注册到列表，无需修改 _merged_capture。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..models.memory_atom import MemoryAtom


@dataclass
class CaptureContext:
    """在 Capture 流水线各步骤间传递的数据"""

    diary_body: str = ""
    raw_atom_dicts: list[dict] = field(default_factory=list)
    atoms: list[MemoryAtom] = field(default_factory=list)
    quality_warnings: list[str] = field(default_factory=list)
    user_id: str = ""
    diary_date: str = ""
    judge_importance: float = 0.5


class CaptureStep(ABC):
    """Capture 流水线中的一个可插拔步骤

    每个步骤接收 CaptureContext，加工后返回（可能被修改的）CaptureContext。
    步骤之间完全解耦，新增步骤只需新建子类并注册到 Capturer 的步骤列表。
    """

    @abstractmethod
    async def process(self, ctx: CaptureContext) -> CaptureContext:
        """处理上下文，返回（可能修改后的）上下文"""
        ...


class QualityCheckStep(CaptureStep):
    """质量校验步骤 — 检测泛化词、空内容等，仅记录警告不拒写"""

    def __init__(self, enable: bool = True):
        self.enable = enable

    async def process(self, ctx: CaptureContext) -> CaptureContext:
        if not self.enable or not ctx.diary_body:
            return ctx
        from .quality_validator import validate_merged_output

        quality = validate_merged_output(
            ctx.diary_body, ctx.raw_atom_dicts, ctx.judge_importance
        )
        import logging
        logger = logging.getLogger("Memory")

        if quality["diary"] == "low":
            logger.warning(
                f"[Memory] 日记质量低: {len(ctx.diary_body)}字, "
                f"内容={ctx.diary_body[:60]}"
            )
            ctx.quality_warnings.append("diary_low")
        if quality["atoms"] == "low":
            logger.warning(
                f"[Memory] 原子质量低: {len(ctx.raw_atom_dicts)}条"
            )
            ctx.quality_warnings.append("atoms_low")
        if quality["generic_terms"]:
            logger.warning(
                f"[Memory] 日记含泛化词: {ctx.diary_body[:80]}"
            )
            ctx.quality_warnings.append("generic_terms")
        return ctx


class AtomClassifyStep(CaptureStep):
    """原子分类步骤 — 规则基或 LLM 驱动

    规则基（默认）: 零 LLM 调用，正则匹配 key_facts 生成 MemoryAtom。
    LLM 驱动（回退）: 使用 _convert_merged_atoms 逻辑（简单类型转换）。
    """

    def __init__(
        self,
        use_rule_classifier: bool = True,
    ):
        self.use_rule_classifier = use_rule_classifier

    async def process(self, ctx: CaptureContext) -> CaptureContext:
        if not ctx.raw_atom_dicts:
            return ctx

        if self.use_rule_classifier:
            from .atom_classifier import classify_atoms

            key_facts = [
                a.get("content", "") for a in ctx.raw_atom_dicts if a.get("content")
            ]
            all_entities = list(set(
                e for a in ctx.raw_atom_dicts for e in a.get("entities", [])
            ))
            atoms = classify_atoms(
                key_facts=key_facts,
                entities=all_entities,
                parent_importance=ctx.judge_importance,
                user_id=ctx.user_id,
                diary_date=ctx.diary_date,
            )
            # 补回 diary_snippet
            snippet_map: dict[str, str] = {}
            for a in ctx.raw_atom_dicts:
                content = a.get("content", "").strip()
                snippet = a.get("diary_snippet", "").strip()
                if content and snippet:
                    snippet_map[content] = snippet
            for atom in atoms:
                if atom.content in snippet_map:
                    atom.diary_snippet = snippet_map[atom.content]
        else:
            # 回退：简单类型转换
            atoms = [
                self._dict_to_atom(a, ctx.user_id, ctx.diary_date)
                for a in ctx.raw_atom_dicts
                if isinstance(a, dict) and a.get("content")
            ]

        ctx.atoms = atoms
        return ctx

    @staticmethod
    def _dict_to_atom(item: dict, user_id: str, diary_date: str) -> MemoryAtom | None:
        from ..models.memory_atom import AtomType
        try:
            return MemoryAtom(
                user_id=user_id,
                diary_date=diary_date,
                content=item.get("content", "").strip(),
                atom_type=AtomType(item.get("type", "unknown")),
                importance=float(item.get("importance", 0.5)),
                entities=item.get("entities", []),
                confidence=float(item.get("confidence", 0.7)),
                diary_snippet=item.get("diary_snippet", ""),
            )
        except (ValueError, TypeError):
            return None


class DiaryFillStep(CaptureStep):
    """日记占位步骤 — diary 为空但有原子时生成简短占位"""

    async def process(self, ctx: CaptureContext) -> CaptureContext:
        if ctx.diary_body or not ctx.atoms:
            return ctx
        entities = list(set(
            e for a in ctx.atoms for e in (a.entities or [])
        ))
        if entities:
            ctx.diary_body = f"与{'、'.join(entities)}的对话。"
        return ctx


class TruncateStep(CaptureStep):
    """截断步骤 — 按重要度降序取 top N"""

    def __init__(self, max_atoms: int = 5):
        self.max_atoms = max_atoms

    async def process(self, ctx: CaptureContext) -> CaptureContext:
        if not ctx.atoms:
            return ctx
        ctx.atoms.sort(key=lambda a: a.importance, reverse=True)
        ctx.atoms = ctx.atoms[:self.max_atoms]
        return ctx
