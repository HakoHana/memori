"""抓取器 — Judge + DiaryWriter + AtomExtractor 三合一"""

from __future__ import annotations

import asyncio
import json
import re
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

        # 功能开关（feature flags）
        self._enable_rule_classifier = self.config.get("enable_rule_classifier", True)
        self._enable_quality_check = self.config.get("enable_quality_check", True)
        self._enable_json_repair = self.config.get("enable_json_repair", True)
        self._enable_dual_summary = self.config.get("enable_dual_summary", False)

        # 加载 prompt 模板（带内存缓存，不重复读文件）
        self._prompts: dict[str, str] = {}
        prompts_path = Path(prompts_dir)
        for name in ("judge", "diary", "atoms", "merged"):
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
        完整抓取流水线（合并模式）：
        一次 LLM 调用同时输出日记 + 原子事实
        """
        today = time.strftime("%Y-%m-%d")

        # 写操作日志（可选）
        op_id = None
        if self.write_op_log:
            op_id = await self.write_op_log.begin("capture", {"user_id": user_id, "date": today})

        # 1. 合并调用：一次 LLM 输出日记 + 原子
        diary_body, raw_atoms = await self._merged_capture(
            judge_result, conversation_summary, user_id, today
        )

        diary_content = ""
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
            if await self._reinforce_if_duplicate(atom, user_id, judge_result.importance, today):
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

        # 3. 异步更新图谱（fire-and-forget，不阻塞 capture 返回）
        #    图谱是 diary 写入的副产物，不需要等待
        if diary_id > 0 and self.on_atoms_created:
            all_entities: list[str] = []
            for atom in atoms:
                all_entities.extend(atom.entities or [])
            try:
                task = asyncio.ensure_future(
                    self.on_atoms_created(diary_id, diary_content, all_entities)
                )
                task.add_done_callback(
                    lambda t: logger.warning(f"[Capturer] 图谱索引异常: {t.exception()}") if t.exception() else None
                )
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

    async def _apply_reinforcement(
        self,
        content: str,
        user_id: str,
        judge_importance: float = 0.5,
        new_confidence: float = 0.7,
        threshold: float = 0.6,
    ) -> tuple[bool, MemoryAtom | None]:
        """核心：检查内容是否与已有记忆重复，重复则强化

        可被前后调用复用：
        - Step 3（LLM 前）：传入消息文本，命中则跳过昂贵模型
        - Step 5（LLM 后）：传入新原子 content，命中则跳过插入

        强化策略：
        - 步长随强化次数递减（首次 +0.05，后续收敛到 0.01）
        - 融合 judge 重要性评分（0.7权重judge + 0.3权重原值）
        - 延长 expires_at 30%
        - 回写源日记 importance

        Returns:
            (True, matched_atom)  — 找到重复并强化
            (False, None)         — 无匹配
        """
        try:
            import math

            chars = content.replace(" ", "")
            if len(chars) < 4:
                return False, None
            tokens = {chars[i:i+2] for i in range(len(chars) - 1)}
            if len(tokens) < 2:
                return False, None

            query = " OR ".join(f'"{t}"' for t in list(tokens)[:6])
            now = time.time()

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
                if jaccard >= threshold:
                    # 步长递减
                    step = max(0.01, 0.05 / math.log2((ex.access_count or 0) + 2))
                    step_boosted = ex.importance + step
                    # 融合 judge
                    judge_blend = judge_importance * 0.7 + ex.importance * 0.3
                    boosted = min(0.95, max(step_boosted, judge_blend))
                    # 延长 expires_at
                    old_expires = max(ex.expires_at, now)
                    new_expires = now + (old_expires - now) * 1.3

                    await self.atom_store.execute(
                        "UPDATE memory_atoms SET importance=?, confidence=?, access_count=access_count+1, expires_at=? WHERE id=?",
                        (boosted, max(new_confidence, ex.confidence), new_expires, ex.atom_id),
                    )

                    # 回写源日记
                    if boosted > ex.importance and ex.diary_id > 0:
                        try:
                            await self.atom_store.execute(
                                "UPDATE diary_entries SET importance = MAX(importance, ?) WHERE id = ?",
                                (boosted, ex.diary_id),
                            )
                        except Exception:
                            pass

                    return True, ex
        except Exception:
            pass
        return False, None

    async def _reinforce_if_duplicate(
        self, atom, user_id: str, judge_importance: float = 0.5, diary_date: str = ""
    ) -> bool:
        """旧接口包装：LLM 后去重强化 + forgotten 清理

        在 _apply_reinforcement 基础上增加：
        - 同 diary_id → 跳过插入（True）
        - forgotten 重复 → 删除旧原子，让新原子替代
        """
        content = atom.content or ""
        matched, ex = await self._apply_reinforcement(content, user_id, judge_importance, atom.confidence)
        if matched:
            # 同 batch 的插入跳过
            if ex.diary_id == atom.diary_id:
                return True
            # 跨 batch 的也跳过（已被更强版本覆盖）
            return True

        # ── forgotten 清理：旧遗忘原子与新原子重复则彻底删除 ──
        try:
            chars = content.replace(" ", "")
            if len(chars) >= 4:
                tokens = {chars[i:i+2] for i in range(len(chars) - 1)}
                if len(tokens) >= 2:
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

        支持格式：
        - [HH:MM] 昵称: 消息
        - [MM-DD HH:MM] 昵称: 消息
        - [MM-DD] 昵称: 消息
        - [昵称]: 消息（旧格式兜底）
        """
        import re
        speakers = set()
        for m in re.finditer(r'^\[[^\]]+\] (.+?):\s', text, re.MULTILINE):
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

    async def _merged_capture(
        self,
        judge: CaptureJudgeResult,
        conversation_summary: str,
        user_id: str,
        diary_date: str,
    ) -> tuple[str, list[MemoryAtom]]:
        """合并调用：一次 LLM 调用同时输出日记 + 原子事实

        使用 merged.txt 提示词，模型返回 {"diary": "...", "atoms": [...]}。

        降级策略：
        1. JSON 解析成功 → 正常返回
        2. JSON 解析失败，但能提取到 diary → diary + 空 atoms
        3. 合并调用的 prompt 不存在 → 回退到旧的分步调用
        4. LLM 调用异常 → 回退到旧的分步调用
        """
        # 如果 merged prompt 不存在，降级到旧的分步调用
        merged_prompt = self._prompts.get("merged", "")
        if not merged_prompt:
            diary_body = await self._write_diary(judge, conversation_summary)
            atoms = await self._extract_atoms(diary_body or (judge.context_summary or ""), user_id, diary_date)
            return diary_body or "", atoms

        # 根据说话人数选择 prompt 变体（复用日记的检测逻辑）
        speaker_count = self._detect_speaker_count(conversation_summary)

        # 填充模板变量
        today = time.strftime("%Y-%m-%d")
        user_prompt = (
            merged_prompt
            .replace("{{current_date}}", today)
            .replace("{{conversation}}", conversation_summary)
            .replace("{{date}}", today)
            .replace("{{reason}}", judge.reason or "")
            .replace("{{mood}}", judge.mood or "")
        )

        try:
            result_str = await self.llm.chat("", user_prompt)
        except Exception as e:
            logger.warning(f"[Memory] 合并调用 LLM 失败，降级到分步: {e}")
            diary_body = await self._write_diary(judge, conversation_summary)
            atoms = await self._extract_atoms(diary_body or (judge.context_summary or ""), user_id, diary_date)
            return diary_body or "", atoms

        # 解析合并响应（带三级降级）
        parsed = self._parse_merged_response(result_str)
        if parsed is None:
            logger.warning("[Memory] 合并 JSON 解析失败，降级到分步")
            diary_body = await self._write_diary(judge, conversation_summary)
            atoms = await self._extract_atoms(diary_body or (judge.context_summary or ""), user_id, diary_date)
            return diary_body or "", atoms

        diary_body = parsed.get("diary", "").strip()
        raw_atom_dicts: list[dict] = parsed.get("atoms", [])

        # ── Phase 3: 质量校验（仅日志，不拒写） ──
        if self._enable_quality_check:
            from .quality_validator import validate_merged_output
            quality = validate_merged_output(diary_body, raw_atom_dicts, judge.importance)
            if quality["diary"] == "low":
                logger.warning(f"[Memory] 日记质量低: {len(diary_body)}字, 内容={diary_body[:60]}")
            if quality["atoms"] == "low":
                logger.warning(f"[Memory] 原子质量低: {len(raw_atom_dicts)}条")
            if quality["generic_terms"]:
                logger.warning(f"[Memory] 日记含泛化词: {diary_body[:80]}")

        # ── Phase 1: 规则基分类或传统 LLM 驱动 ──
        if self._enable_rule_classifier:
            from .atom_classifier import classify_atoms
            # 提取 key_facts（content 列表）和 entities
            key_facts = [a.get("content", "") for a in raw_atom_dicts if a.get("content")]
            all_entities = list(set(
                e for a in raw_atom_dicts for e in a.get("entities", [])
            ))
            raw_atoms = classify_atoms(
                key_facts=key_facts,
                entities=all_entities,
                parent_importance=judge.importance,
                user_id=user_id,
                diary_date=diary_date,
            )
            # 把 diary_snippet 从原始 dict 中补回（classifier 不处理这个字段）
            snippet_map: dict[str, str] = {}
            for a in raw_atom_dicts:
                content = a.get("content", "").strip()
                snippet = a.get("diary_snippet", "").strip()
                if content and snippet:
                    snippet_map[content] = snippet
            for atom in raw_atoms:
                if atom.content in snippet_map:
                    atom.diary_snippet = snippet_map[atom.content]
        else:
            raw_atoms = self._convert_merged_atoms(raw_atom_dicts, user_id, diary_date)

        # 如果 diary 为空但 atoms 存在，给一个简短的占位 diary
        if not diary_body and raw_atoms:
            diary_body = f"与{'、'.join(set(e for a in raw_atoms for e in (a.entities or [])))}的对话。"

        # 按重要度降序取 top 5
        raw_atoms.sort(key=lambda a: a.importance, reverse=True)
        atoms = raw_atoms[:5]

        logger.info(f"[Memory] 合并调用成功: diary={len(diary_body)}字, atoms={len(atoms)}条")
        return diary_body, atoms

    def _parse_merged_response(self, text: str) -> dict | None:
        """解析合并调用返回的 JSON，三级降级

        1. 完整 JSON 解析（含 _fix_json 修复）
        2. 能提取到 diary 字段（atoms 可能残缺）
        3. 完全失败返回 None
        """
        cleaned = text.strip()
        # 去掉 markdown 代码块包裹（_fix_json 也会做，先做一次方便后续降级）
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        cleaned = cleaned.strip()

        # 一级：先修复再解析
        fixed = self._fix_json(cleaned)
        try:
            data = json.loads(fixed)
            if isinstance(data, dict) and "diary" in data:
                # 验证 atoms 结构
                atoms_ok = True
                if "atoms" in data and isinstance(data["atoms"], list):
                    for a in data["atoms"]:
                        if not isinstance(a, dict) or "content" not in a:
                            atoms_ok = False
                            break
                    if not atoms_ok:
                        data["atoms"] = []
                return data
        except json.JSONDecodeError:
            pass

        # 二级：从文本中提取 JSON 对象
        brace_start = cleaned.find("{")
        brace_end = cleaned.rfind("}")
        if brace_start >= 0 and brace_end > brace_start:
            try:
                data = json.loads(cleaned[brace_start: brace_end + 1])
                if isinstance(data, dict) and "diary" in data:
                    if "atoms" not in data or not isinstance(data.get("atoms"), list):
                        data["atoms"] = []
                    return data
            except json.JSONDecodeError:
                pass

        # 三级：尝试分别提取 diary 和 atoms
        diary = self._extract_diary_fallback(cleaned)
        atoms = self._extract_atoms_fallback(cleaned)
        if diary:
            return {"diary": diary, "atoms": atoms or []}

        return None

    @staticmethod
    def _extract_diary_fallback(text: str) -> str | None:
        """降级提取 diary 文本"""
        patterns = [
            r'["""]?diary["""]?\s*[:：]\s*["""](.*?)["""](?=\s*[,]\s*["""]?atoms["""]?\s*[:：])',
            r'(?:日记|diary)[:：]\s*(.*?)(?=\n\s*(?:原子|事实|atoms)\s*[:：])',
            r'(?:日记|diary)[:：]\s*(.*?)(?=\n\s*[\[\{])',
        ]
        for pat in patterns:
            m = re.search(pat, text, re.DOTALL | re.IGNORECASE)
            if m:
                content = m.group(1).strip().strip('"\'')
                if len(content) > 10:
                    return content
        # 兜底：取第一段非空文本（≥30字）
        for para in text.split("\n\n"):
            para = para.strip()
            if len(para) > 30 and "{" not in para[:5]:
                return para
        return None

    @staticmethod
    def _extract_atoms_fallback(text: str) -> list[dict] | None:
        """降级提取 atoms"""
        # 找 JSON 数组
        bracket_start = text.find("[")
        bracket_end = text.rfind("]")
        if bracket_start >= 0 and bracket_end > bracket_start:
            try:
                atoms = json.loads(text[bracket_start: bracket_end + 1])
                if isinstance(atoms, list) and all(isinstance(a, dict) for a in atoms):
                    return atoms
            except json.JSONDecodeError:
                pass
        return None

    def _convert_merged_atoms(
        self, raw_atoms: list[dict], user_id: str, diary_date: str
    ) -> list[MemoryAtom]:
        """将合并响应中的 atoms 转换为 MemoryAtom 对象"""
        atoms: list[MemoryAtom] = []
        for item in raw_atoms:
            if not isinstance(item, dict) or "content" not in item:
                continue
            content = item.get("content", "").strip()
            if not content:
                continue
            try:
                atoms.append(MemoryAtom(
                    user_id=user_id,
                    diary_date=diary_date,
                    content=content,
                    atom_type=AtomType(item.get("type", "unknown")),
                    importance=float(item.get("importance", 0.5)),
                    entities=item.get("entities", []),
                    confidence=float(item.get("confidence", 0.7)),
                    diary_snippet=item.get("diary_snippet", ""),
                ))
            except (ValueError, TypeError):
                continue
        return atoms

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

    @staticmethod
    def _fix_json(text: str) -> str:
        """尝试修复损坏的 JSON 字符串

        修复项：
        - 移除 markdown 代码块
        - 修复未闭合的引号
        - 修复未闭合的方括号和花括号
        - 移除尾部逗号
        - 处理转义字符

        Args:
            text: 可能损坏的 JSON 字符串

        Returns:
            修复后的字符串
        """
        fixed = text.strip()

        # 移除 markdown 代码块标记
        if fixed.startswith("```json"):
            fixed = fixed[7:]
        elif fixed.startswith("```"):
            fixed = fixed[3:]
        if fixed.endswith("```"):
            fixed = fixed[:-3]
        fixed = fixed.strip()

        # 修复未闭合的字符串（截断的 JSON）
        open_quotes = fixed.count('"') - fixed.count('\\"')
        if open_quotes % 2 != 0:
            fixed += '"'

        # 修复未闭合的数组
        open_brackets = fixed.count("[") - fixed.count("]")
        if open_brackets > 0:
            fixed += "]" * open_brackets

        # 修复未闭合的对象
        open_braces = fixed.count("{") - fixed.count("}")
        if open_braces > 0:
            fixed += "}" * open_braces

        # 移除尾部逗号（JSON 不允许）
        fixed = re.sub(r",(\s*[}\]])", r"\1", fixed)

        # 修复常见的转义问题
        fixed = fixed.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")

        return fixed
