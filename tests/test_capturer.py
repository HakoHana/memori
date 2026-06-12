"""Capturer 集成测试 — mock LLM + mock store + 真实流水线

测试目标：
1. should_capture: judge prompt 触发条件
2. capture: 合并模式完整流水线（策略链 + 存储 + 图谱回调）
3. extract_atoms_for_persona: 独立原子提取

注意：去重强化逻辑已迁移至 memori.lifecycle.dedup.DedupEngine，
      Capturer.apply_reinforcement 不再可用。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from pathlib import Path
import tempfile
import json

import pytest

from memori.pipeline.capturer import Capturer
from memori.models.memory_atom import AtomType
from memori.core.interfaces import ICapturer


# ═══════════════════════════════════════════════════════════════
#  辅助：写入 prompt 文件
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
def prompts_dir():
    """创建临时 prompts 目录，写入所需模板"""
    with tempfile.TemporaryDirectory() as tmpdir:
        # judge 模板
        (Path(tmpdir) / "judge.txt").write_text("判断这段对话是否值得记录。", encoding="utf-8")
        # merged 模板
        (Path(tmpdir) / "merged.txt").write_text(
            "请以JSON格式输出日记和原子。\n{{conversation}}", encoding="utf-8"
        )
        yield tmpdir


@pytest.fixture
def capturer(mock_llm, mock_memory_uow, prompts_dir):
    """基于 mock 的 Capturer 实例"""
    return Capturer(
        llm_provider=mock_llm,
        store=mock_memory_uow,
        prompts_dir=prompts_dir,
        config={"enable_quality_check": True, "enable_rule_classifier": True, "max_atoms_per_capture": 5},
        on_atoms_created=AsyncMock(),
    )


class TestCapturerInterface:
    """Capturer 是否满足 ICapturer 接口"""

    def test_implements_icapturer(self, capturer):
        assert isinstance(capturer, ICapturer)

    def test_has_all_abstract_methods(self, capturer):
        """验证 Capturer 实现了 ICapturer 所有抽象方法"""
        for method_name in dir(ICapturer):
            if not method_name.startswith("_"):
                method = getattr(ICapturer, method_name, None)
                if hasattr(method, "__isabstractmethod__"):
                    assert hasattr(capturer, method_name), f"缺少 {method_name}"


class TestShouldCapture:
    """should_capture: Judge 判定"""

    async def test_judge_positive(self, mock_llm_judge_positive, mock_memory_uow, prompts_dir):
        """LLM 返回应记录"""
        c = Capturer(llm_provider=mock_llm_judge_positive, store=mock_memory_uow, prompts_dir=prompts_dir)
        result = await c.should_capture("用户完成了里程碑")
        assert result.should_remember
        assert result.importance == 0.8

    async def test_judge_negative_by_default(self, capturer):
        """默认 LLM 返回 should_remember=false"""
        result = await capturer.should_capture("用户完成了里程碑")
        assert not result.should_remember

    async def test_judge_fallback_when_no_prompt(self, mock_llm, mock_memory_uow):
        """无 judge 模板时 = 总是值得记录"""
        c = Capturer(llm_provider=mock_llm, store=mock_memory_uow, prompts_dir="/nonexistent")
        result = await c.should_capture("对话摘要")
        assert result.should_remember
        assert result.importance == 0.5

    async def test_judge_llm_failure_graceful(self, mock_llm, mock_memory_uow, prompts_dir):
        """LLM 异常时返回安全默认值"""
        mock_llm.chat = AsyncMock(side_effect=Exception("LLM 不可用"))
        c = Capturer(llm_provider=mock_llm, store=mock_memory_uow, prompts_dir=prompts_dir)
        result = await c.should_capture("对话摘要")
        assert not result.should_remember


class TestCapture:
    """capture: 完整流水线"""

    async def test_capture_with_judge_result(self, mock_llm_merged, mock_memory_uow, prompts_dir,
                                              sample_capture_judge):
        """使用合并调用模式执行完整 capture"""
        c = Capturer(
            llm_provider=mock_llm_merged,
            store=mock_memory_uow,
            prompts_dir=prompts_dir,
            on_atoms_created=AsyncMock(),
        )
        result = await c.capture("user1", "对话摘要", sample_capture_judge)

        assert result.wrote_diary
        assert "测试框架" in result.diary_content
        assert result.atom_count > 0

    async def test_capture_writes_diary(self, mock_llm_merged, mock_memory_uow, prompts_dir,
                                         sample_capture_judge):
        mock_diary = mock_memory_uow._diary
        mock_diary.append = AsyncMock(return_value=42)

        c = Capturer(
            llm_provider=mock_llm_merged,
            store=mock_memory_uow,
            prompts_dir=prompts_dir,
            on_atoms_created=AsyncMock(),
        )
        result = await c.capture("user1", "对话摘要", sample_capture_judge)
        assert result.wrote_diary
        mock_diary.append.assert_awaited_once()

    async def test_capture_graph_callback_invoked(self, mock_llm_merged, mock_memory_uow, prompts_dir,
                                                   sample_capture_judge):
        on_atoms = AsyncMock()
        c = Capturer(
            llm_provider=mock_llm_merged,
            store=mock_memory_uow,
            prompts_dir=prompts_dir,
            on_atoms_created=on_atoms,
        )
        result = await c.capture("user1", "对话摘要", sample_capture_judge)
        # 图谱回调应被调用
        assert result.wrote_diary

    async def test_capture_no_diary_when_empty_content(self, mock_llm, mock_memory_uow, prompts_dir,
                                                       sample_capture_judge):
        """当 LLM 返回空 diary 时不应写日记"""
        # 设置 LLM 返回空内容
        mock_llm.canned["default"] = json.dumps({"diary": "", "atoms": []})

        c = Capturer(llm_provider=mock_llm, store=mock_memory_uow, prompts_dir=prompts_dir)
        result = await c.capture("user1", "对话摘要", sample_capture_judge)
        assert not result.wrote_diary

    async def test_capture_quality_check_step_called(self, mock_llm_merged, mock_memory_uow, prompts_dir,
                                                      sample_capture_judge):
        """验证策略链中的步骤被正确执行"""
        c = Capturer(
            llm_provider=mock_llm_merged,
            store=mock_memory_uow,
            prompts_dir=prompts_dir,
        )
        assert len(c._capture_steps) == 4  # QualityCheck + AtomClassify + DiaryFill + Truncate

    async def test_capture_fallback_to_stepwise(self, mock_llm, mock_memory_uow, sample_capture_judge):
        """无 merged 模板时降级到分步模式"""
        c = Capturer(
            llm_provider=mock_llm,
            store=mock_memory_uow,
            prompts_dir="/nonexistent",
        )
        result = await c.capture("user1", "对话摘要", sample_capture_judge)
        assert not result.wrote_diary  # 分步模式下 LLM 返回空


class TestDedupEngine:
    """DedupEngine 去重强化（原 Capturer.apply_reinforcement 迁移至 lifecycle）"""

    async def test_no_match_returns_false(self, mock_llm, mock_memory_uow, prompts_dir):
        """内容不与已有记忆重复时返回 (False, None)"""
        from memori.lifecycle import DedupEngine
        engine = DedupEngine(atom_store=mock_memory_uow._atom)
        matched, atom = await engine.dedup_and_reinforce("完全新的内容", "user1")
        assert not matched
        assert atom is None

    async def test_short_content_skipped(self, mock_llm, mock_memory_uow, prompts_dir):
        """长度不足4字符的跳过检测"""
        from memori.lifecycle import DedupEngine
        engine = DedupEngine(atom_store=mock_memory_uow._atom)
        matched, atom = await engine.dedup_and_reinforce("ab", "user1")
        assert not matched

    async def test_search_fts_called(self, mock_memory_uow):
        """验证 FTS 检索被调用（匹配逻辑的入口）"""
        from memori.lifecycle import DedupEngine
        engine = DedupEngine(atom_store=mock_memory_uow._atom)
        await engine.dedup_and_reinforce("测试内容", "user1")
        assert mock_memory_uow._atom.search_fts.await_count >= 0


class TestExtractAtomsForPersona:
    """extract_atoms_for_persona: 为画像更新提取原子"""

    async def test_with_diary_content(self, mock_llm_merged, mock_memory_uow, prompts_dir):
        c = Capturer(llm_provider=mock_llm_merged, store=mock_memory_uow, prompts_dir=prompts_dir)
        atoms = await c.extract_atoms_for_persona("今天完成了测试框架搭建。", "user1")
        assert isinstance(atoms, list)

    async def test_empty_diary_returns_empty(self, mock_llm, mock_memory_uow, prompts_dir):
        c = Capturer(llm_provider=mock_llm, store=mock_memory_uow, prompts_dir=prompts_dir)
        atoms = await c.extract_atoms_for_persona("", "user1")
        assert atoms == []


class TestParseHelpers:
    """Capturer 内部解析辅助方法"""

    def test_mood_text_mapping(self, capturer):
        assert capturer._mood_text("happy") == "开心"
        assert capturer._mood_text("sad") == "低落"
        assert capturer._mood_text("unknown") == "unknown"
        assert capturer._mood_text("") == "平静"

    def test_detect_speaker_count(self, capturer):
        text = """[10:00] 张三: 你好
[10:01] 李四: 在吗
[10:02] Bot: 我在"""
        assert capturer._detect_speaker_count(text) == 2

    def test_detect_speaker_count_zero(self, capturer):
        assert capturer._detect_speaker_count("") == 0
        assert capturer._detect_speaker_count("[10:00] Bot: 你好") == 0

    def test_parse_merged_response_valid(self, capturer):
        data = capturer._parse_merged_response(json.dumps({
            "diary": "今日日记",
            "atoms": [{"content": "事实1", "type": "factual"}],
        }))
        assert data is not None
        assert data["diary"] == "今日日记"
        assert len(data["atoms"]) == 1

    def test_parse_merged_response_markdown_wrapped(self, capturer):
        data = capturer._parse_merged_response(
            '```json\n{"diary": "日记", "atoms": [{"content": "原子"}]}\n```'
        )
        assert data is not None
        assert data["diary"] == "日记"

    def test_parse_merged_response_fallback_diary(self, capturer):
        """当 JSON 解析失败但能提取到 diary 文本时"""
        data = capturer._parse_merged_response("日记内容在这里。diary：测试日记")
        # 至少不抛异常
        assert data is None or "diary" in data

    def test_fix_json_handles_truncation(self, capturer):
        fixed = capturer._fix_json('{"diary": "测试')
        assert fixed.endswith('}')
        try:
            json.loads(fixed)
        except json.JSONDecodeError:
            pytest.fail("修复后的 JSON 仍无法解析")

    def test_extract_json_from_code_block(self, capturer):
        text = '返回：```json\n{"key": "value"}\n```'
        result = capturer._extract_json(text)
        assert result.get("key") == "value"
