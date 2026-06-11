"""质量校验层单元测试 — 纯函数，无外部依赖"""

from __future__ import annotations

import pytest

from memori.pipeline.quality_validator import (
    validate_diary_quality,
    validate_atoms_quality,
    has_generic_terms,
    has_real_name,
    validate_merged_output,
)
from memori.models.memory_atom import MemoryAtom


class TestValidateDiaryQuality:
    def test_empty_diary_is_low(self):
        assert validate_diary_quality("") == "low"
        assert validate_diary_quality("   ") == "low"

    def test_short_diary_is_low(self):
        assert validate_diary_quality("你好") == "low"

    def test_meaningful_diary_is_normal(self):
        assert validate_diary_quality("今天用户完成了测试框架的搭建。") == "normal"

    def test_custom_min_length(self):
        assert validate_diary_quality("hi", min_length=2) == "normal"


class TestValidateAtomsQuality:
    def test_no_atoms_is_low(self):
        assert validate_atoms_quality([]) == "low"
        assert validate_atoms_quality(None) == "low"  # type: ignore

    def test_meaningful_atoms_is_normal(self):
        atoms = [MemoryAtom(user_id="u1", diary_date="2026-06-11", content="有效原子")]
        assert validate_atoms_quality(atoms) == "normal"

    def test_short_content_skipped(self):
        atoms = [MemoryAtom(user_id="u1", diary_date="2026-06-11", content="ab")]
        assert validate_atoms_quality(atoms) == "low"

    def test_dict_atoms_supported(self):
        atoms = [{"content": "有效的原子内容"}]
        assert validate_atoms_quality(atoms) == "normal"

    def test_custom_min_count(self):
        atoms = [MemoryAtom(user_id="u1", diary_date="2026-06-11", content="有效的原子内容")]
        assert validate_atoms_quality(atoms, min_count=2) == "low"
        atoms2 = [
            MemoryAtom(user_id="u1", diary_date="2026-06-11", content="有效内容A"),
            MemoryAtom(user_id="u1", diary_date="2026-06-11", content="有效内容B"),
        ]
        assert validate_atoms_quality(atoms2, min_count=2) == "normal"


class TestHasGenericTerms:
    """泛化词检测"""

    def test_no_generic_terms(self):
        assert not has_generic_terms("今天张三完成了工作。")

    def test_with_generic_terms(self):
        assert has_generic_terms("今天某用户完成了工作。")
        assert has_generic_terms("有人提到了一个重要的话题。")
        assert has_generic_terms("该用户说了一些事情。")
        assert has_generic_terms("the user mentioned something.")

    def test_empty_text(self):
        assert not has_generic_terms("")
        assert not has_generic_terms(None)  # type: ignore


class TestHasRealName:
    """中文真实姓名检测"""

    def test_no_name(self):
        assert not has_real_name("今天天气不错。")

    def test_with_real_name(self):
        assert has_real_name("张三说这个功能做完了。")
        assert has_real_name("李四提到了新的需求。")
        assert has_real_name("王五来了。")

    def test_surname_only_not_match(self):
        # 单姓不匹配（需姓氏+至少1字名）
        assert not has_real_name("张")

    def test_empty_text(self):
        assert not has_real_name("")


class TestValidateMergedOutput:
    """validate_merged_output 整体校验"""

    def test_all_normal(self):
        result = validate_merged_output(
            diary="今天完成了有意义的工作。",
            atoms=[{"content": "有效原子"}],
            importance=0.8,
        )
        assert result["diary"] == "normal"
        assert result["atoms"] == "normal"
        assert result["importance"] == "normal"
        assert not result["generic_terms"]

    def test_diary_low(self):
        result = validate_merged_output(
            diary="短",
            atoms=[{"content": "有效原子"}],
        )
        assert result["diary"] == "low"

    def test_atoms_low(self):
        result = validate_merged_output(
            diary="正常长度的日记内容。",
            atoms=[],
        )
        assert result["atoms"] == "low"

    def test_importance_low(self):
        result = validate_merged_output(
            diary="正常长度的日记内容。",
            atoms=[{"content": "有效原子"}],
            importance=1.5,  # 超出范围
        )
        assert result["importance"] == "low"

    def test_generic_terms_detected(self):
        result = validate_merged_output(
            diary="今天某用户完成了工作。",
            atoms=[{"content": "有效原子"}],
        )
        assert result["generic_terms"]
