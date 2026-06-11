"""MemoryAtom 模型、原子生命周期、衰减计算 单元测试"""

from __future__ import annotations

import time

import pytest

from memori.models.memory_atom import (
    AtomType,
    AtomStatus,
    DecayType,
    MemoryAtom,
    CaptureResult,
    CaptureJudgeResult,
    RecallResult,
    compute_expires_at,
    compute_decay_score,
)


class TestComputeExpiresAt:
    """compute_expires_at: 不同 atom_type 的 TTL 和衰减类型是否正确"""

    def test_episodic_default(self):
        expires_at, decay = compute_expires_at(AtomType.EPISODIC, importance=0.5)
        assert decay == DecayType.EXPONENTIAL
        expected_ttl = 30 * 86400 * (0.5 + 0.5)  # base_ttl=30, importance_factor=1.0
        assert abs(expires_at - (time.time() + expected_ttl)) < 2

    def test_factual_long_ttl(self):
        expires_at, decay = compute_expires_at(AtomType.FACTUAL, importance=1.0)
        assert decay == DecayType.EXPONENTIAL
        expected_ttl = 180 * 86400 * (0.5 + 1.0)  # base_ttl=180, imp_factor=1.5
        assert abs(expires_at - (time.time() + expected_ttl)) < 2

    def test_planned_step_decay(self):
        _, decay = compute_expires_at(AtomType.PLANNED, importance=0.5)
        assert decay == DecayType.STEP

    def test_relational_linear_decay(self):
        _, decay = compute_expires_at(AtomType.RELATIONAL, importance=0.5)
        assert decay == DecayType.LINEAR

    def test_custom_ttl_overrides_default(self):
        expires_at, decay = compute_expires_at(AtomType.EPISODIC, importance=0.5, ttl_days=10)
        assert decay == DecayType.EXPONENTIAL
        expected_ttl = 10 * 86400 * (0.5 + 0.5)
        assert abs(expires_at - (time.time() + expected_ttl)) < 2

    def test_importance_boosts_ttl(self):
        low_imp, _ = compute_expires_at(AtomType.FACTUAL, importance=0.0)
        high_imp, _ = compute_expires_at(AtomType.FACTUAL, importance=1.0)
        assert high_imp > low_imp  # higher importance → longer TTL


class TestComputeDecayScore:
    """compute_decay_score: 三种衰减类型的数学正确性"""

    def test_fresh_atom_score_one(self):
        """刚创建的原子衰减分数应为 1.0"""
        assert compute_decay_score(DecayType.EXPONENTIAL, ttl_days=30, age_days=0) == 1.0
        assert compute_decay_score(DecayType.LINEAR, ttl_days=30, age_days=0) == 1.0
        assert compute_decay_score(DecayType.STEP, ttl_days=30, age_days=0) == 1.0

    def test_exponential_decay(self):
        """指数衰减：半衰期=TTL/2，到达半衰期时应约为 0.5"""
        score = compute_decay_score(DecayType.EXPONENTIAL, ttl_days=30, age_days=15)
        assert 0.45 <= score <= 0.55  # 半衰期附近

    def test_linear_decay(self):
        """线性衰减：age=TTL/2 → score=0.5"""
        score = compute_decay_score(DecayType.LINEAR, ttl_days=30, age_days=15)
        assert score == pytest.approx(0.5)

    def test_step_decay_before_ttl(self):
        """阶梯衰减：未达到 TTL 前 score=1.0"""
        score = compute_decay_score(DecayType.STEP, ttl_days=30, age_days=25)
        assert score == 1.0

    def test_step_decay_after_ttl(self):
        """阶梯衰减：超过 TTL → score=0.05"""
        score = compute_decay_score(DecayType.STEP, ttl_days=30, age_days=31)
        assert score == 0.05

    def test_linear_decay_past_ttl(self):
        """线性衰减超过 TTL → 0"""
        score = compute_decay_score(DecayType.LINEAR, ttl_days=30, age_days=60)
        assert score == 0.0

    def test_exponential_decay_long_term(self):
        """指数衰减长期趋近 0 但不为 0"""
        score = compute_decay_score(DecayType.EXPONENTIAL, ttl_days=30, age_days=365)
        assert 0 < score < 0.01

    def test_min_ttl_clamp(self):
        """ttl_days 为 0 时使用 1.0 避免除零"""
        score = compute_decay_score(DecayType.LINEAR, ttl_days=0, age_days=0.5)
        assert score == 0.5

    def test_negative_age_zero(self):
        """age_days 为负时按 0 处理"""
        score = compute_decay_score(DecayType.LINEAR, ttl_days=30, age_days=-5)
        assert score == 1.0


class TestMemoryAtom:
    """MemoryAtom 生命周期：创建 → prepare_insert → 过期检测"""

    def test_create_atom_defaults(self):
        atom = MemoryAtom(user_id="u1", diary_date="2026-06-11")
        assert atom.user_id == "u1"
        assert atom.content == ""
        assert atom.atom_type == AtomType.UNKNOWN
        assert atom.importance == 0.5
        assert atom.confidence == 0.7
        assert atom.status == AtomStatus.ACTIVE
        assert atom.atom_id == 0

    def test_prepare_insert_sets_expires_at(self):
        atom = MemoryAtom(
            user_id="u1",
            diary_date="2026-06-11",
            content="测试内容",
            atom_type=AtomType.EPISODIC,
            importance=0.8,
        )
        assert atom.expires_at == 0  # 插入前为 0
        atom.prepare_insert()
        assert atom.expires_at > time.time()  # 插入后为未来时间
        assert atom.decay_type == DecayType.EXPONENTIAL

    def test_is_expired_false_when_not_set(self):
        atom = MemoryAtom(user_id="u1", diary_date="2026-06-11")
        assert not atom.is_expired  # expires_at=0 → 永不过期

    def test_is_expired_true_after_expiry(self):
        atom = MemoryAtom(user_id="u1", diary_date="2026-06-11")
        atom.expires_at = time.time() - 1  # 已过期的未来 = 现在  (过期)
        # 注意 expires_at 是过期时间戳，超过此时间即为过期
        assert atom.is_expired

    def test_is_expired_false_before_expiry(self):
        atom = MemoryAtom(user_id="u1", diary_date="2026-06-11")
        atom.expires_at = time.time() + 99999  # 远未来
        assert not atom.is_expired

    def test_decay_score_fresh(self):
        atom = MemoryAtom(user_id="u1", diary_date="2026-06-11")
        atom.created_at = time.time()
        score = atom.decay_score
        assert score == pytest.approx(1.0, abs=0.01)

    def test_decay_score_after_long_time(self):
        atom = MemoryAtom(user_id="u1", diary_date="2026-06-11")
        atom.created_at = time.time() - 86400 * 365  # 1年前
        score = atom.decay_score
        assert score < 0.5

    def test_serialize_entities(self):
        atom = MemoryAtom(
            user_id="u1",
            diary_date="2026-06-11",
            content="实体测试",
            entities=["张三", "测试框架"],
        )
        assert len(atom.entities) == 2
        assert "张三" in atom.entities

    def test_from_dict_construction(self):
        """验证 MemoryAtom 可以从 dict 参数构造（供 _convert_merged_atoms 使用）"""
        atom = MemoryAtom(
            user_id="u1",
            diary_date="2026-06-11",
            content="dict 构建测试",
            atom_type=AtomType.FACTUAL,
            importance=0.7,
            entities=["项目A"],
            confidence=0.85,
            diary_snippet="测试片段",
        )
        assert atom.diary_snippet == "测试片段"
        assert atom.atom_type == AtomType.FACTUAL
        assert atom.importance == 0.7


class TestCaptureResult:
    """CaptureResult 数据类"""

    def test_empty_capture(self):
        result = CaptureResult()
        assert not result.wrote_diary
        assert result.diary_content == ""
        assert result.atom_count == 0

    def test_with_atoms(self):
        atoms = [
            MemoryAtom(user_id="u1", diary_date="2026-06-11", content="原子1"),
            MemoryAtom(user_id="u1", diary_date="2026-06-11", content="原子2"),
        ]
        result = CaptureResult(wrote_diary=True, diary_content="日记内容", atoms=atoms)
        assert result.wrote_diary
        assert result.diary_content == "日记内容"
        assert result.atom_count == 2


class TestCaptureJudgeResult:
    """CaptureJudgeResult 数据类"""

    def test_default_no_remember(self):
        result = CaptureJudgeResult()
        assert not result.should_remember
        assert result.importance == 0.0

    def test_positive_judge(self):
        result = CaptureJudgeResult(
            should_remember=True,
            reason="用户分享了重要进展",
            importance=0.8,
            mood="happy",
            context_summary="用户说…",
        )
        assert result.should_remember
        assert result.importance == 0.8


class TestRecallResult:
    """RecallResult 数据类"""

    def test_default_empty(self):
        result = RecallResult()
        assert result.memory_text == ""
        assert result.atoms == []
        assert result.persona_text is None

    def test_with_data(self):
        atoms = [MemoryAtom(user_id="u1", diary_date="2026-06-11", content="事实")]
        result = RecallResult(
            memory_text="【记忆中最近的事】\n- 事实",
            atoms=atoms,
            persona_text="用户画像",
        )
        assert "事实" in result.memory_text
        assert result.persona_text == "用户画像"
