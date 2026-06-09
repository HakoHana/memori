"""抓取器 — Judge + DiaryWriter + AtomExtractor 三合一"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .logger import logger

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
        on_atoms_created: callable = None,
        write_op_log=None,
    ):
        self.llm = llm_provider
        self.diary_store = diary_store
        self.atom_store = atom_store
        self.config = config or {}
        self.max_diary_tokens = self.config.get("max_diary_tokens", 500)
        self.on_atoms_created = on_atoms_created
        self.write_op_log = write_op_log

        # 加载 prompt 模板（带内存缓存，不重复读文件）
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
            return CaptureJudgeResult(
                should_remember=True,
                importance=0.5,
                mood="neutral",
                context_summary=conversation_summary,
            )
        try:
            system = self._prompts["judge"]
            result_str = await self.llm.chat(system, conversation_summary, use_judge=True)
            return self._parse_judge_result(result_str, conversation_summary)
        except Exception as e:
            logger.warning(f"[Memory] Judge LLM 调用失败: {e}")
            return CaptureJudgeResult(should_remember=False)

    async def capture(
        self, user_id: str, conversation_summary: str, judge_result: CaptureJudgeResult
    ) -> CaptureResult:
        """
        完整抓取流水线：
        1. 写日记（获取日记 ID）
        2. 提取原子（存入 SQLite）
        """
        today = time.strftime("%Y-%m-%d")

        # 写操作日志（可选）
        op_id = None
        if self.write_op_log:
            op_id = await self.write_op_log.begin("capture", {"user_id": user_id, "date": today})

        # 1. 写日记（获取日记 ID）
        diary_body = await self._write_diary(judge_result, conversation_summary)
        diary_id = 0
        if diary_body:
            # 用 frontmatter 包装（自描述格式）
            from .diary_helper import build_diary_content
            fm = {
                "date": today,
                "mood": self._mood_text(judge_result.mood),
                "importance": judge_result.importance,
                "topics": [judge_result.reason] if judge_result.reason else [],
            }
            diary_content = build_diary_content(fm, diary_body)
            diary_id = await self.diary_store.append(user_id, today, diary_content)
            if op_id:
                await self.write_op_log.step(op_id, "diary_written")

        # 2. 提取原子（从 body 提取，不含 frontmatter）
        atom_source = diary_body if diary_body else (judge_result.context_summary or "")
        raw_atoms = await self._extract_atoms(atom_source, user_id, today)

        # 2a. 去重强化 + 限 5 条
        existing_count = 0
        if diary_id > 0:
            row = await self.atom_store.fetchone(
                "SELECT COUNT(*) FROM memory_atoms WHERE diary_id=? AND status='active'",
                (diary_id,)
            )
            existing_count = row[0] if row else 0

        slots_left = max(0, 5 - existing_count)
        unique_atoms: list = []
        for atom in raw_atoms:
            atom.diary_id = diary_id
            atom.prepare_insert()
            if await self._reinforce_if_duplicate(atom, user_id):
                continue  # 已强化已有原子，不插入
            if len(unique_atoms) >= slots_left:
                continue  # 超过 5 条上限，跳过
            unique_atoms.append(atom)

        atoms = unique_atoms
        if atoms:
            ids = await self.atom_store.insert_many(atoms)
            for atom, aid in zip(atoms, ids):
                atom.atom_id = aid
            if op_id:
                await self.write_op_log.step(op_id, "atoms_stored")

            # 同步写入全局事实表（去重）
            if diary_id > 0:
                max_imp = 0.5
                for atom in atoms:
                    try:
                        fact_id = await self.atom_store.ensure_fact(
                            content=atom.content,
                            atom_type=atom.atom_type.value,
                            importance=atom.importance,
                            confidence=atom.confidence,
                        )
                        await self.atom_store.link_fact(
                            diary_id=diary_id,
                            fact_id=fact_id,
                            importance=atom.importance,
                            snippet=atom.diary_snippet,
                        )
                        if atom.importance > max_imp:
                            max_imp = atom.importance
                    except Exception:
                        pass
                try:
                    await self.diary_store.update_metadata(
                        user_id, today, importance=max_imp
                    )
                except Exception:
                    pass

        # 3. 更新图谱（从 diary content 解析 [[链接]]，原子 entities 合并进入）
        #    图谱是 diary 写入的副产物，不需要独立调用
        if diary_id > 0 and self.on_atoms_created:
            all_entities: list[str] = []
            for atom in atoms:
                all_entities.extend(atom.entities or [])
            try:
                await self.on_atoms_created(diary_id, diary_content, all_entities)
            except Exception:
                pass

        if op_id:
            await self.write_op_log.complete(op_id)

        return CaptureResult(
            wrote_diary=bool(diary_content),
            diary_content=diary_content or "",
            atoms=atoms,
        )

    async def extract_atoms_for_persona(
        self, diary_content: str, user_id: str
    ) -> list[MemoryAtom]:
        """为画像更新提取原子（独立于日记流程）"""
        today = time.strftime("%Y-%m-%d")
        return await self._extract_atoms(diary_content, user_id, today)

    async def _reinforce_if_duplicate(self, atom, user_id: str) -> bool:
        """检查原子是否与已有原子相似，相似则强化，否则清除遗忘同类"""
        try:
            content = atom.content or ""
            chars = content.replace(" ", "")
            if len(chars) < 4:
                return False
            tokens = {chars[i:i+2] for i in range(len(chars) - 1)}
            if len(tokens) < 2:
                return False

            query = " OR ".join(f'"{t}"' for t in list(tokens)[:6])

            # ── 检查 active 重复 → 强化 ──
            existing = await self.atom_store.search_fts(query, user_id, k=5)
            for ex in existing:
                ex_chars = (ex.content or "").replace(" ", "")
                if len(ex_chars) < 4:
                    continue
                ex_tokens = {ex_chars[i:i+2] for i in range(len(ex_chars) - 1)}
                if not ex_tokens:
                    continue
                union = len(tokens | ex_tokens)
                if union == 0:
                    continue
                jaccard = len(tokens & ex_tokens) / union
                if jaccard >= 0.6:
                    boosted = min(1.0, ex.importance + 0.05)
                    await self.atom_store.execute(
                        "UPDATE memory_atoms SET importance=?, confidence=?, access_count=access_count+1 WHERE id=?",
                        (boosted, max(atom.confidence, ex.confidence), ex.atom_id),
                    )
                    if ex.diary_id == atom.diary_id:
                        return True

            # ── 检查 forgotten 重复 → 删除旧原子，让新原子替代 ──
            for uid in [user_id, "Hako"]:
                try:
                    rows = await self.atom_store.fetch(
                        "SELECT id, content FROM memory_atoms WHERE user_id=? AND status='forgotten' AND diary_id=?",
                        (uid, atom.diary_id),
                    )
                    for r in rows:
                        old_c = (r[1] or "").replace(" ", "")
                        if len(old_c) < 4:
                            continue
                        old_tokens = {old_c[i:i+2] for i in range(len(old_c) - 1)}
                        if not old_tokens:
                            continue
                        u2 = len(tokens | old_tokens)
                        if u2 == 0:
                            continue
                        if len(tokens & old_tokens) / u2 >= 0.6:
                            await self.atom_store.execute("DELETE FROM memory_atoms WHERE id=?", (r[0],))
                            await self.atom_store.execute("DELETE FROM memory_atoms_fts WHERE atom_id=?", (r[0],))
                except Exception:
                    pass
        except Exception:
            pass
        return False

    @staticmethod
    def _mood_text(mood: str) -> str:
        """将 mood 转为人可读的标签"""
        mapping = {
            "happy": "开心", "sad": "低落", "angry": "生气",
            "excited": "兴奋", "neutral": "平静", "mixed": "复杂",
        }
        return mapping.get(mood.strip().lower(), mood.strip() or "平静")

    @staticmethod
    def _detect_speaker_count(text: str) -> int:
        """从对话文本中检测说话人数（去重后的非 Bot 说话者数量）

        格式：[昵称]: 消息 或 [昵称 | ID: xxx | 时间] 消息
        """
        import re
        speakers = set()
        for m in re.finditer(r'^\[([^\]|:]+)(?:\s*[|:]\s*|:\s*)', text, re.MULTILINE):
            name = m.group(1).strip()
            if not name.lower().startswith("bot"):
                speakers.add(name)
        return len(speakers)

    async def _write_diary(
        self,
        judge: CaptureJudgeResult,
        conversation_summary: str,
    ) -> str:
        """LLM 写日记（根据对话中说话人数选择提示词）

        - 多人（≥2 非 Bot 说话者）→ group_chat 提示词
        - 单人（0~1）→ private_chat 提示词
        """
        speaker_count = self._detect_speaker_count(conversation_summary)
        prompt_key = "group_chat" if speaker_count >= 2 else "private_chat"
        prompt = self._prompts.get(prompt_key, "")
        if not prompt:
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
        except Exception as e:
            logger.warning(f"[Memory] 写日记 LLM 失败: {e}")
            return ""

    async def _extract_atoms(
        self, diary_content: str, user_id: str, diary_date: str
    ) -> list[MemoryAtom]:
        """LLM 从日记内容中提取原子

        限制：最多返回 5 条最重要的原子（按重要度排序取 top 5）
        """
        prompt = self._prompts.get("atoms", "")
        if not prompt or not diary_content:
            return []

        try:
            result_str = await self.llm.chat(prompt, diary_content)
            atoms = self._parse_atoms(result_str, user_id, diary_date)
            # 按重要度降序取 top 5
            atoms.sort(key=lambda a: a.importance, reverse=True)
            return atoms[:5]
        except Exception as e:
            logger.warning(f"[Memory] 提取原子 LLM 失败: {e}")
            return []

    def _parse_judge_result(self, text: str, default_summary: str) -> CaptureJudgeResult:
        """解析 LLM 返回的判断 JSON"""
        try:
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

    def _parse_atoms(self, text: str, user_id: str, diary_date: str) -> list[MemoryAtom]:
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
        # 1. 直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # 2. ```json ... ```
        if "```json" in text:
            start = text.index("```json") + 7
            end = text.index("```", start) if "```" in text[start:] else len(text)
            try:
                return json.loads(text[start:end].strip())
            except json.JSONDecodeError:
                pass
        # 3. { ... } 截取
        brace_start = text.find("{")
        brace_end = text.rfind("}")
        if brace_start >= 0 and brace_end > brace_start:
            try:
                return json.loads(text[brace_start: brace_end + 1])
            except json.JSONDecodeError:
                pass
        # 4. [ ... ] 截取
        bracket_start = text.find("[")
        bracket_end = text.rfind("]")
        if bracket_start >= 0 and bracket_end > bracket_start:
            try:
                return json.loads(text[bracket_start: bracket_end + 1])
            except json.JSONDecodeError:
                pass
        return {}
