"""抓取器 — Judge + DiaryWriter + AtomExtractor 三合一"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ..models.memory_atom import (
    MemoryAtom,
    AtomType,
    CaptureJudgeResult,
    CaptureResult,
)
from ..storage.diary_store import DiaryStore
from ..storage.atom_store import AtomStore
from .adapters import LLMProvider


class Capturer:
    """
    抓取器：从对话中提取记忆

    被 ConsolidationManager 调用，作为 L1Runner 运行完整流水线。
    三步走：判断值不值得记 → 写日记 → 提取原子
    """

    def __init__(
        self,
        llm_provider: LLMProvider,
        diary_store: DiaryStore,
        atom_store: AtomStore,
        prompts_dir: str,
        config: dict[str, Any] | None = None,
        on_atoms_created: callable = None,  # 回调：原子创建后通知图谱引擎
    ):
        self.llm = llm_provider
        self.diary_store = diary_store
        self.atom_store = atom_store
        self.config = config or {}
        self.max_diary_tokens = self.config.get("max_diary_tokens", 500)
        self.on_atoms_created = on_atoms_created

        # 加载 prompt 模板
        self._prompts: dict[str, str] = {}
        prompts_path = Path(prompts_dir)
        for name in ("judge", "diary", "atoms"):
            pf = prompts_path / f"{name}.txt"
            if pf.exists():
                self._prompts[name] = pf.read_text(encoding="utf-8")
            else:
                self._prompts[name] = ""

    async def should_capture(self, conversation_summary: str) -> CaptureJudgeResult:
        """判断这段对话值不值得记"""
        if not self._prompts.get("judge"):
            # 没有 prompt 模板则默认记
            return CaptureJudgeResult(
                should_remember=True,
                importance=0.5,
                mood="neutral",
                context_summary=conversation_summary,
            )
        try:
            system = self._prompts["judge"]
            result_str = await self.llm.chat(system, conversation_summary)
            return self._parse_judge_result(result_str, conversation_summary)
        except Exception:
            # LLM 失败时保守处理：不记
            return CaptureJudgeResult(should_remember=False)

    async def capture(
        self, user_id: str, conversation_summary: str, judge_result: CaptureJudgeResult
    ) -> CaptureResult:
        """
        完整抓取流水线：
        1. 写日记（追加到当日 .md）
        2. 提取原子（存入 SQLite）
        """
        today = time.strftime("%Y-%m-%d")

        # 1. 写日记
        diary_content = await self._write_diary(
            user_id, judge_result, conversation_summary
        )
        if diary_content:
            await self.diary_store.append(user_id, today, diary_content)

        # 2. 提取原子
        atoms = await self._extract_atoms(diary_content, user_id, today)
        for atom in atoms:
            atom.prepare_insert()  # 计算 expires_at 和 decay_type
        if atoms:
            ids = await self.atom_store.insert_many(atoms)
            for atom, aid in zip(atoms, ids):
                atom.atom_id = aid

            # 通知图谱引擎
            if self.on_atoms_created:
                try:
                    for atom in atoms:
                        await self.on_atoms_created(atom)
                except Exception:
                    pass

        return CaptureResult(
            wrote_diary=bool(diary_content),
            diary_content=diary_content or "",
            atoms=atoms,
        )

    async def extract_atoms_for_persona(
        self, diary_content: str, user_id: str
    ) -> list[MemoryAtom]:
        """
        为画像更新提取原子（独立于日记流程）
        PersonaEngine 调用此方法
        """
        today = time.strftime("%Y-%m-%d")
        return await self._extract_atoms(diary_content, user_id, today)

    async def _write_diary(
        self,
        user_id: str,
        judge: CaptureJudgeResult,
        conversation_summary: str,
    ) -> str:
        """LLM 写日记"""
        prompt = self._prompts.get("diary", "")
        if not prompt:
            return ""

        user_prompt = prompt.replace("{{conversation}}", conversation_summary)
        if judge.mood:
            user_prompt = user_prompt.replace("{{mood}}", judge.mood)
        if judge.reason:
            user_prompt = user_prompt.replace("{{reason}}", judge.reason)

        try:
            return await self.llm.chat("", user_prompt)
        except Exception:
            return ""

    async def _extract_atoms(
        self, diary_content: str, user_id: str, diary_date: str
    ) -> list[MemoryAtom]:
        """LLM 从日记内容中提取原子"""
        prompt = self._prompts.get("atoms", "")
        if not prompt or not diary_content:
            return []

        try:
            result_str = await self.llm.chat(prompt, diary_content)
            return self._parse_atoms(result_str, user_id, diary_date)
        except Exception:
            return []

    def _parse_judge_result(
        self, text: str, default_summary: str
    ) -> CaptureJudgeResult:
        """解析 LLM 返回的判断 JSON"""
        try:
            # 尝试提取 JSON
            data = self._extract_json(text)
            return CaptureJudgeResult(
                should_remember=data.get("should_remember", False),
                reason=data.get("reason", ""),
                importance=float(data.get("importance", 0.5)),
                mood=data.get("mood", ""),
                context_summary=data.get("context_summary", default_summary),
            )
        except Exception:
            return CaptureJudgeResult(should_remember=False)

    def _parse_atoms(
        self, text: str, user_id: str, diary_date: str
    ) -> list[MemoryAtom]:
        """解析 LLM 返回的原子 JSON"""
        atoms = []
        try:
            data = self._extract_json(text)
            raw_atoms = data.get("atoms", []) if isinstance(data, dict) else data
            if isinstance(raw_atoms, list):
                for item in raw_atoms:
                    if isinstance(item, dict) and "content" in item:
                        atoms.append(MemoryAtom(
                            user_id=user_id,
                            diary_date=diary_date,
                            content=item["content"],
                            atom_type=AtomType(item.get("type", "unknown")),
                            importance=float(item.get("importance", 0.5)),
                            entities=item.get("entities", []),
                            confidence=float(item.get("confidence", 0.7)),
                            diary_snippet=item.get("diary_snippet", ""),
                        ))
        except Exception:
            pass
        return atoms

    def _extract_json(self, text: str) -> dict | list:
        """从 LLM 回复中提取 JSON 对象"""
        text = text.strip()
        # 尝试直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # 尝试从 ```json ... ``` 中提取
        if "```json" in text:
            start = text.index("```json") + 7
            end = text.index("```", start) if "```" in text[start:] else len(text)
            try:
                return json.loads(text[start:end].strip())
            except json.JSONDecodeError:
                pass
        # 尝试从 { 到 } 提取
        brace_start = text.find("{")
        brace_end = text.rfind("}")
        if brace_start >= 0 and brace_end > brace_start:
            try:
                return json.loads(text[brace_start : brace_end + 1])
            except json.JSONDecodeError:
                pass
        # 尝试从 [ 到 ] 提取（列表格式）
        bracket_start = text.find("[")
        bracket_end = text.rfind("]")
        if bracket_start >= 0 and bracket_end > bracket_start:
            try:
                return json.loads(text[bracket_start : bracket_end + 1])
            except json.JSONDecodeError:
                pass
        return {}
