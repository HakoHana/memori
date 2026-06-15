"""抓取器 — Judge + DiaryWriter + AtomExtractor 三合一"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

from ..core.logger import logger

from ..models.memory_atom import (
    MemoryAtom,
    AtomType,
    CaptureJudgeResult,
    CaptureResult,
)
from ..core.adapters import LLMProvider, EmbeddingProvider
from ..core.interfaces import ICapturer
from .capture_step import (
    CaptureStep, CaptureContext,
    QualityCheckStep, AtomClassifyStep, DiaryFillStep, TruncateStep,
)
from .memory_uow import MemoryUnitOfWork



class Capturer(ICapturer):
    """
    抓取器：从对话中提取记忆

    被 ConsolidationManager 调用，作为 L1Runner 运行完整流水线。
    三步走：判断值不值得记 → 写日记 → 提取原子
    """

    def __init__(
        self,
        llm_provider: LLMProvider,
        store: MemoryUnitOfWork,
        prompts_dir: str,
        config: dict[str, Any] | None = None,
        on_atoms_created: callable = None,
        embed_provider: EmbeddingProvider | None = None,
    ):
        self.llm = llm_provider
        self._store = store
        self.config = config or {}
        self.max_diary_tokens = self.config.get("max_diary_tokens", 500)
        self.on_atoms_created = on_atoms_created
        self.embed_provider = embed_provider
        self.atom_store = store._atom
        self._entity_uid_cache: dict[str, str] = {}  # 显示名 → canonical uid（懒加载）
        self.lifecycle = None  # LifecycleManager（由外部注入，无则跳过去重）

        # 构建 Capture 流水线步骤（策略链）
        # 新增步骤只需新建 CaptureStep 子类并注册到此列表
        self._capture_steps: list[CaptureStep] = []
        self._capture_steps.append(QualityCheckStep(
            enable=self.config.get("enable_quality_check", True),
        ))
        self._capture_steps.append(AtomClassifyStep(
            use_rule_classifier=self.config.get("enable_rule_classifier", True),
            entity_uid_cache=self._entity_uid_cache,
        ))
        self._capture_steps.append(DiaryFillStep())
        self._capture_steps.append(TruncateStep(
            max_atoms=self.config.get("max_atoms_per_capture", 5),
        ))
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
            result_str = await self.llm.chat_with_judge(system, conversation_summary)
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
        op_id = await self._store.begin_op("capture", {"user_id": user_id, "date": today})

        # 1. 合并调用：一次 LLM 输出日记 + 原子
        diary_body, raw_atoms = await self._merged_capture(
            judge_result, conversation_summary, user_id, today
        )

        diary_content = ""
        diary_id = 0
        if diary_body:
            # 用 frontmatter 包装（自描述格式）
            from ..utils.diary_helper import build_diary_content
            fm = {
                "date": today,
                "mood": self._mood_text(judge_result.mood),
                "importance": judge_result.importance,
                "topics": [judge_result.reason] if judge_result.reason else [],
            }
            diary_content = build_diary_content(fm, diary_body)
            diary_id = await self._store.append_diary(today, diary_content)
            if op_id:
                await self._store.step_op(op_id, "diary_written")

        # 2a. 全局去重 + 桥表关联（原子事实是全局的，跨用户去重）
        atoms: list[MemoryAtom] = []
        for atom in raw_atoms:
            atom.prepare_insert()
            if self.lifecycle:
                matched, ex = await self.lifecycle.dedup_and_reinforce(
                    atom.content or "", "global",
                    judge_importance=judge_result.importance,
                    new_confidence=atom.confidence,
                )
                if matched and ex and diary_id > 0:
                    # 已存在，强化后桥表关联到当前日记
                    await self._store.atom_store.link_atom_to_diary(
                        ex.atom_id, diary_id,
                        snippet=atom.diary_snippet,
                        importance=atom.importance,
                    )
                    continue
                if not matched:
                    atoms.append(atom)
            else:
                atoms.append(atom)
            if len(atoms) >= 5:
                break  # 限 5 条

        # 2b. 算 embedding（如有 provider，供语义去重 + 入库持久化）
        _emb_model = ""
        if self.embed_provider and atoms:
            try:
                texts = [a.content[:512] for a in atoms if a.content]
                if texts:
                    embeddings = await self.embed_provider.embed_batch(texts)
                    _emb_model = type(self.embed_provider).__name__
                    for atom, emb in zip(atoms, embeddings):
                        if emb:
                            atom.embedding = emb
            except Exception as e:
                logger.warning(f"[Capturer] Embedding 计算失败（跳过语义去重）: {e}")

        # 2c. 语义去重（Jieba → FTS 粗召回 → Jaccard 筛选 → 余弦精排）
        if self.lifecycle and self.lifecycle.dedup and atoms and _emb_model:
            try:
                atoms = await self.lifecycle.dedup.semantic_dedup_new_atoms(
                    atoms, diary_id, _emb_model, threshold=0.90,
                )
            except Exception as e:
                logger.warning(f"[Capturer] 语义去重异常（跳过，继续入库）: {e}")

        if atoms:
            ids = await self._store.insert_atoms(atoms)
            for atom, aid in zip(atoms, ids):
                atom.atom_id = aid
                # 新原子桥表关联到当前日记
                if diary_id > 0:
                    try:
                        await self._store.atom_store.link_atom_to_diary(
                            aid, diary_id, snippet=atom.diary_snippet, importance=atom.importance,
                        )
                    except Exception:
                        pass
                # 持久化 embedding（2b 已算好，写入内存，此处写库）
                if getattr(atom, 'embedding', None) and atom.atom_id > 0:
                    try:
                        await self._store.update_embedding(
                            atom.atom_id, atom.embedding, _emb_model,
                        )
                    except Exception:
                        pass
            if op_id:
                await self._store.step_op(op_id, "atoms_stored")

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
            await self._store.complete_op(op_id)

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

    async def _compute_embeddings(self, atoms: list[MemoryAtom]):
        """异步计算并持久化原子 embedding

        Capture 流程的异步后处理步骤，不阻塞主流程。
        仅在 embed_provider 配置时自动执行。

        语义去重由梦境界面定时扫描全库完成，此处不重复做。
        """
        if not self.embed_provider or not atoms:
            return
        try:
            texts = [a.content for a in atoms if a.content]
            if not texts:
                return
            embeddings = await self.embed_provider.embed_batch(texts)
            model_name = type(self.embed_provider).__name__
            for atom, emb in zip(atoms, embeddings):
                if atom.atom_id > 0:
                    atom.embedding = emb
                    await self._store.update_embedding(atom.atom_id, emb, model_name)
            logger.info(
                f"[Capturer] 已计算 {len(embeddings)} 条原子 embedding "
                f"(model={model_name}, dim={len(embeddings[0]) if embeddings else 0})"
            )
        except Exception as e:
            logger.warning(f"[Capturer] 计算 embedding 失败: {e}")

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
            atoms = await self._parse_atoms(result_str, user_id, diary_date)
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

        # ── 执行策略链：各 CaptureStep 依次加工上下文 ──
        ctx = CaptureContext(
            diary_body=diary_body,
            raw_atom_dicts=raw_atom_dicts,
            user_id=user_id,
            diary_date=diary_date,
            judge_importance=judge.importance,
        )
        for step in self._capture_steps:
            ctx = await step.process(ctx)

        diary_body, atoms = ctx.diary_body, ctx.atoms

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

    async def _resolve_entity_uid(self, entity_name: str) -> str | None:
        """根据实体名查 canonical uid"""
        if entity_name in self._entity_uid_cache:
            return self._entity_uid_cache[entity_name]
        try:
            row = await self.atom_store.fetchone(
                "SELECT uid FROM canonical_users WHERE primary_name=?", (entity_name,)
            )
            if row:
                self._entity_uid_cache[entity_name] = row[0]
                return row[0]
            self._entity_uid_cache[entity_name] = ""  # 缓存未命中
        except Exception:
            pass
        return None

    async def _pick_atom_user_id(self, entities: list, trigger_uid: str) -> str:
        """从实体列表选出原子的归属 uid（谁的事实）"""
        if not entities:
            return trigger_uid
        for ent in entities:
            if isinstance(ent, str):
                uid = self._entity_uid_cache.get(ent)
                if uid is None:  # 未查过
                    uid = await self._resolve_entity_uid(ent)
                if uid and uid != trigger_uid:
                    return uid
        return trigger_uid

    async def _convert_merged_atoms(
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
                entities = item.get("entities", [])
                atom_uid = await self._pick_atom_user_id(entities, user_id)
                atoms.append(MemoryAtom(
                    user_id=atom_uid,
                    diary_date=diary_date,
                    content=content,
                    atom_type=AtomType(item.get("type", "unknown")),
                    importance=float(item.get("importance", 0.5)),
                    entities=entities,
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

    async def _parse_atoms(self, text: str, user_id: str, diary_date: str) -> list[MemoryAtom]:
        """解析 LLM 返回的原子 JSON"""
        atoms = []
        try:
            data = self._extract_json(text)
            raw_atoms = data.get("atoms", []) if isinstance(data, dict) else data
            if isinstance(raw_atoms, list):
                for item in raw_atoms:
                    if isinstance(item, dict) and "content" in item:
                        entities = item.get("entities", [])
                        atom_uid = await self._pick_atom_user_id(entities, user_id)
                        atoms.append(MemoryAtom(
                            user_id=atom_uid,
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
