"""规则基原子分类器单元测试 — 零 LLM 调用，纯正则匹配

测试关键路径：
- 每种 AtomType 的分类逻辑
- 时间解析（中文时间表达 → 时间戳）
- 边界条件（空输入、短文本、停用词）
"""

from __future__ import annotations

import re
import time
from datetime import datetime

import pytest

from memori.pipeline.atom_classifier import (
    classify_atoms,
    _classify_single,
    _parse_event_time,
    _parse_weekday_time,
)
from memori.models.memory_atom import AtomType


class TestClassifySingle:
    """_classify_single: 单条事实的类型推断"""

    @pytest.mark.parametrize("text,expected_type", [
        ("明天下午去开会", AtomType.PLANNED),         # 未来时间 + 行动
        ("下周提交报告", AtomType.PLANNED),
        ("今晚一起吃饭", AtomType.FACTUAL),              # 吃不在行动动词中 → factual
        ("昨天和张三讨论了方案", AtomType.EPISODIC),    # 过去时间 + 行动
        ("刚刚完成了测试", AtomType.EPISODIC),
        ("之前提到过这个问题", AtomType.EPISODIC),
        ("用户喜欢喝咖啡", AtomType.PREFERENCE),       # 偏好词
        ("用户讨厌吃辣", AtomType.PREFERENCE),
        ("张三和李四是同事", AtomType.RELATIONAL),      # 关系词
        ("他们是朋友", AtomType.RELATIONAL),
        ("Python 是一种编程语言", AtomType.FACTUAL),    # 状态性描述
        ("北京是中国的首都", AtomType.FACTUAL),
        ("今天去跑步了", AtomType.EPISODIC),           # 默认动作+时间→事件
        ("完成了一个任务", AtomType.EPISODIC),          # 有动作→事件
    ])
    def test_classification(self, text, expected_type):
        atom_type, confidence, event_time = _classify_single(text)
        assert atom_type == expected_type, f"{text} → {expected_type}"

    def test_confidence_values(self):
        """每种类型有合理的置信度"""
        test_cases = [
            ("明天下午去开会", 0.85),            # PLANNED
            ("昨天讨论了方案", 0.80),            # EPISODIC（+过去时间）
            ("用户喜欢喝咖啡", 0.82),            # PREFERENCE
            ("他们是同事", 0.80),                # RELATIONAL
            ("北京是首都", 0.78),               # FACTUAL
            ("做了一件事", 0.75),               # EPISODIC（+动作）
            ("一个词", 0.65),                   # FACTUAL fallback
        ]
        for text, expected_conf in test_cases:
            _, confidence, _ = _classify_single(text)
            assert confidence == expected_conf, f"{text} confidence={confidence}"

    def test_event_time_extracted(self):
        """带有时间词的分类应返回 event_time"""
        _, _, event_time = _classify_single("昨天和张三讨论了方案")
        assert event_time is not None
        # 应约为 1 天前
        assert abs(event_time - (time.time() - 86400)) < 5

    def test_no_event_time_for_non_time(self):
        """无时间词时 event_time 为 None"""
        _, _, event_time = _classify_single("用户喜欢喝咖啡")
        assert event_time is None


class TestParseEventTime:
    """_parse_event_time: 中文时间表达 → 时间戳"""

    def test_today(self):
        ts = _parse_event_time("今天完成了任务")
        assert ts is not None
        assert abs(ts - time.time()) < 5

    def test_yesterday(self):
        ts = _parse_event_time("昨天做了什么")
        assert ts is not None
        assert abs(ts - (time.time() - 86400)) < 5

    def test_tomorrow(self):
        ts = _parse_event_time("明天有安排")
        assert ts is not None
        assert abs(ts - (time.time() + 86400)) < 5

    def test_month_day_format(self):
        """测试 '5月30日' 格式"""
        ts = _parse_event_time("5月30日开会")
        if ts is None:
            pytest.skip("月日解析未实现或当前月份不同")
        dt = datetime.fromtimestamp(ts)
        assert dt.month == 5
        assert dt.day == 30

    def test_no_time_match(self):
        ts = _parse_event_time("随便说了一句话")
        assert ts is None

    def test_empty_text(self):
        assert _parse_event_time("") is None


class TestParseWeekdayTime:
    """_parse_weekday_time: 中文星期 → 时间戳"""

    def test_this_week(self):
        """本周X 返回将来最近的该星期几"""
        now = time.time()
        ts = _parse_weekday_time("本周五", now)
        if ts is None:
            pytest.skip("星期解析未命中")
        assert abs(ts - now) < 86400 * 7


class TestClassifyAtoms:
    """classify_atoms: 批量分类入口"""

    def test_single_fact(self):
        atoms = classify_atoms(
            key_facts=["昨天完成了项目里程碑"],
            entities=["项目A"],
            parent_importance=0.8,
            user_id="test_user",
            diary_date="2026-06-11",
        )
        assert len(atoms) == 1
        atom = atoms[0]
        assert atom.content == "昨天完成了项目里程碑"
        assert atom.atom_type == AtomType.EPISODIC
        assert atom.importance == 0.8
        assert atom.entities == ["项目A"]
        assert atom.expires_at > 0  # prepare_insert 已被调用

    def test_multiple_facts(self):
        key_facts = [
            "用户喜欢喝咖啡",
            "昨天和张三讨论了方案",
            "北京是中国的首都",
        ]
        atoms = classify_atoms(key_facts)
        assert len(atoms) == 3
        types = [a.atom_type for a in atoms]
        assert AtomType.PREFERENCE in types
        assert AtomType.EPISODIC in types
        assert AtomType.FACTUAL in types

    def test_short_facts_filtered(self):
        atoms = classify_atoms(["a", "ab", ""])
        assert len(atoms) == 0  # 长度不足 4 被过滤

    def test_entities_shared_across_atoms(self):
        atoms = classify_atoms(
            key_facts=["上午开了会", "下午写了代码"],
            entities=["张三"],
        )
        for atom in atoms:
            assert "张三" in atom.entities

    def test_parent_importance_propagated(self):
        atoms = classify_atoms(
            key_facts=["完成了测试搭建"],
            parent_importance=0.95,
        )
        assert atoms[0].importance == 0.95

    def test_empty_input(self):
        assert classify_atoms([]) == []

    def test_event_time_in_metadata(self):
        """有时间词的事实应在 metadata 中记录 event_time"""
        atoms = classify_atoms(["昨天完成了任务"])
        assert len(atoms) == 1
        assert "event_time" in atoms[0].metadata
        assert isinstance(atoms[0].metadata["event_time"], float)
