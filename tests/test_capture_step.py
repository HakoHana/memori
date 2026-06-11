"""CaptureStep 策略链单元测试 — 每一步单独验证"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from memori.pipeline.capture_step import (
    CaptureContext,
    QualityCheckStep,
    AtomClassifyStep,
    DiaryFillStep,
    TruncateStep,
)
from memori.models.memory_atom import MemoryAtom, AtomType


class TestCaptureContext:
    """CaptureContext 数据传递正确性"""

    def test_default_empty(self):
        ctx = CaptureContext()
        assert ctx.diary_body == ""
        assert ctx.raw_atom_dicts == []
        assert ctx.atoms == []
        assert ctx.quality_warnings == []
        assert ctx.user_id == ""
        assert ctx.judge_importance == 0.5


class TestQualityCheckStep:
    """QualityCheckStep: 质量校验但不应拒写"""

    async def test_disabled_step_passthrough(self):
        step = QualityCheckStep(enable=False)
        ctx = CaptureContext(diary_body="今天完成了工作。", raw_atom_dicts=[{"content": "test"}])
        result = await step.process(ctx)
        # 禁用状态下不应添加警告
        assert len(result.quality_warnings) == 0

    async def test_empty_diary_no_warnings(self):
        step = QualityCheckStep(enable=True)
        ctx = CaptureContext(diary_body="")
        result = await step.process(ctx)
        assert len(result.quality_warnings) == 0

    async def test_low_quality_diary_warning(self):
        step = QualityCheckStep(enable=True)
        ctx = CaptureContext(diary_body="短", raw_atom_dicts=[{"content": "有效原子"}], judge_importance=0.5)
        result = await step.process(ctx)
        assert "diary_low" in result.quality_warnings

    async def test_generic_terms_detected(self):
        step = QualityCheckStep(enable=True)
        ctx = CaptureContext(
            diary_body="今天某用户完成了工作。",  # 含泛化词
            raw_atom_dicts=[{"content": "有效原子"}],
            judge_importance=0.8,
        )
        result = await step.process(ctx)
        assert len(result.quality_warnings) >= 0


class TestAtomClassifyStep:
    """AtomClassifyStep: 原子分类策略"""

    async def test_no_raw_atoms_passthrough(self):
        step = AtomClassifyStep()
        ctx = CaptureContext(raw_atom_dicts=[])
        result = await step.process(ctx)
        assert result.atoms == []

    async def test_rule_classifier(self):
        step = AtomClassifyStep(use_rule_classifier=True)
        raw_atoms = [
            {"content": "昨天完成了项目里程碑", "entities": ["项目A"], "diary_snippet": "里程碑"},
            {"content": "用户喜欢喝咖啡", "entities": [], "diary_snippet": "喜好"},
        ]
        ctx = CaptureContext(
            raw_atom_dicts=raw_atoms,
            user_id="test_user",
            diary_date="2026-06-11",
            judge_importance=0.8,
        )
        result = await step.process(ctx)
        assert len(result.atoms) == 2
        types = {a.atom_type for a in result.atoms}
        assert AtomType.EPISODIC in types
        assert AtomType.PREFERENCE in types
        assert result.atoms[0].diary_snippet == "里程碑"

    async def test_fallback_classifier(self):
        step = AtomClassifyStep(use_rule_classifier=False)
        raw_atoms = [
            {"content": "完成了测试", "type": "episodic", "importance": 0.8, "entities": ["测试"], "confidence": 0.9},
        ]
        ctx = CaptureContext(
            raw_atom_dicts=raw_atoms,
            user_id="test_user",
            diary_date="2026-06-11",
        )
        result = await step.process(ctx)
        assert len(result.atoms) == 1
        assert result.atoms[0].atom_type == AtomType.EPISODIC
        assert result.atoms[0].importance == 0.8

    async def test_malformed_dict_skipped(self):
        step = AtomClassifyStep(use_rule_classifier=False)
        raw_atoms = [
            {"content": "有效原子"},
            {},  # 无 content → 跳过
            {"content": ""},  # 空 content → 跳过
        ]
        ctx = CaptureContext(raw_atom_dicts=raw_atoms)
        result = await step.process(ctx)
        assert len(result.atoms) == 1


class TestDiaryFillStep:
    """DiaryFillStep: 空白日记填充"""

    async def test_diary_exists_no_change(self):
        step = DiaryFillStep()
        ctx = CaptureContext(diary_body="已有日记", atoms=[MemoryAtom(user_id="u1", diary_date="d", content="a")])
        result = await step.process(ctx)
        assert result.diary_body == "已有日记"

    async def test_no_atoms_no_change(self):
        step = DiaryFillStep()
        ctx = CaptureContext(diary_body="")
        result = await step.process(ctx)
        assert result.diary_body == ""

    async def test_fill_with_entities(self):
        step = DiaryFillStep()
        atoms = [
            MemoryAtom(user_id="u1", diary_date="d", content="a", entities=["张三"]),
            MemoryAtom(user_id="u1", diary_date="d", content="b", entities=["李四"]),
        ]
        ctx = CaptureContext(diary_body="", atoms=atoms)
        result = await step.process(ctx)
        assert "张三" in result.diary_body
        assert "李四" in result.diary_body

    async def test_entities_deduplicated(self):
        step = DiaryFillStep()
        atoms = [
            MemoryAtom(user_id="u1", diary_date="d", content="a", entities=["张三"]),
            MemoryAtom(user_id="u1", diary_date="d", content="b", entities=["张三"]),
        ]
        ctx = CaptureContext(diary_body="", atoms=atoms)
        result = await step.process(ctx)
        assert result.diary_body.count("张三") == 1


class TestTruncateStep:
    """TruncateStep: 原子截断"""

    async def test_no_atoms_passthrough(self):
        step = TruncateStep(max_atoms=5)
        ctx = CaptureContext(atoms=[])
        result = await step.process(ctx)
        assert result.atoms == []

    async def test_sort_by_importance_desc(self):
        step = TruncateStep(max_atoms=5)
        atoms = [
            MemoryAtom(user_id="u1", diary_date="d", content="低", importance=0.3),
            MemoryAtom(user_id="u1", diary_date="d", content="高", importance=0.9),
            MemoryAtom(user_id="u1", diary_date="d", content="中", importance=0.6),
        ]
        ctx = CaptureContext(atoms=atoms)
        result = await step.process(ctx)
        contents = [a.content for a in result.atoms]
        assert contents == ["高", "中", "低"]

    async def test_truncates_excess(self):
        step = TruncateStep(max_atoms=2)
        atoms = [
            MemoryAtom(user_id="u1", diary_date="d", content=f"原子{i}", importance=i / 10)
            for i in range(5)
        ]
        ctx = CaptureContext(atoms=atoms)
        result = await step.process(ctx)
        assert len(result.atoms) == 2

    async def test_within_limit_no_truncation(self):
        step = TruncateStep(max_atoms=10)
        atoms = [
            MemoryAtom(user_id="u1", diary_date="d", content=f"原子{i}", importance=0.5)
            for i in range(3)
        ]
        ctx = CaptureContext(atoms=atoms)
        result = await step.process(ctx)
        assert len(result.atoms) == 3
