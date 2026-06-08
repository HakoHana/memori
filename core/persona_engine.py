"""画像引擎 — L3Runner（增量 + 全量双模式）"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..logger import logger
from ..storage.atom_store import AtomStore
from ..storage.diary_store import DiaryStore
from .adapters import LLMProvider
from .capturer import Capturer


_INC_PROMPT = """当前用户画像（截至 {old_timestamp}）：
{old_summary}

当前标签：{old_tags}

最近新增的日记（最多5篇）：
{new_diaries}

最近新增的原子事实（最多10条）：
{new_facts}

请基于以上新增信息，输出对用户画像的增量修改（JSON格式）：
{{
  "add": ["新增的特征1", "新增的特征2"],
  "modify": [{{"old": "原描述片段", "new": "修正后的描述"}}],
  "delete": ["完全过时的特征"],
  "tags": ["标签1", "标签2", "标签3"]
}}
注意：tags 是 3~5 个最能概括用户特征的关键词，如 ["技术", "Python", "本地部署"]。
只输出变化部分，不要输出未变化的原有内容。"""


class PersonaEngine:
    """
    用户画像引擎 — L3Runner

    双模式：
    - 增量更新（默认）：旧画像 + 新增内容 → LLM diff → 应用
    - 全量重建（手动）：从 L1+L2 重新生成

    存储：user_persona SQLite 表（替代旧 personas/*.md）
    """

    def __init__(
        self,
        llm_provider: LLMProvider,
        atom_store: AtomStore,
        diary_store: DiaryStore,
        capturer: Capturer,
        prompts_dir: str,
        config: dict[str, Any] | None = None,
    ):
        self.llm = llm_provider
        self.atom_store = atom_store
        self.diary_store = diary_store
        self.capturer = capturer
        self.config = config or {}

        prompt_path = Path(prompts_dir) / "persona.txt"
        self._prompt_full = (
            prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else ""
        )
        self._prompt_inc = _INC_PROMPT
        self._cache: dict[str, str | None] = {}
        self._cache_time: float = 0

    # ═══════════════════════════════════════════════════
    #  读取（带缓存）
    # ═══════════════════════════════════════════════════

    async def get_persona(self, uid: str) -> str | None:
        """获取用户画像摘要（60s LRU 缓存）"""
        import time
        now = time.time()
        cached = self._cache.get(uid, ...)
        if cached is not ... and now - self._cache_time < 60:
            return cached

        summary = await self.atom_store.get_persona_summary(uid)
        self._cache[uid] = summary
        self._cache_time = now
        return summary

    # ═══════════════════════════════════════════════════
    #  增量更新（默认，低成本）
    # ═══════════════════════════════════════════════════

    async def incremental_update(self, uid: str, new_diaries: list[str] | None = None,
                                  new_facts: list[str] | None = None) -> bool:
        """增量更新：旧画像 + 新增内容 → LLM diff → 应用

        不传 new_diaries/new_facts 时自动从数据库查询最近内容。
        """
        old_summary = await self.atom_store.get_persona_summary(uid) or "（还没有画像）"

        # 没有传入新数据时自动查询
        if not new_diaries and not new_facts:
            recent = await self.diary_store.fetch("""
                SELECT content FROM diary_entries
                WHERE user_id=? ORDER BY id DESC LIMIT 5
            """, (uid,))
            new_diaries = [(r[0] or "")[:200] for r in recent]
            # 没有事实更新时先跳过（增量更新只在新内容出现时才有意义）
            if not new_diaries:
                return False

        old_row = await self.atom_store.fetchone(
            "SELECT updated_at, tags FROM user_persona WHERE uid=?", (uid,)
        )
        old_ts = old_row[0] if old_row else "从未"
        old_tags = ""
        if old_row and old_row[1]:
            try:
                import json
                old_tags = ", ".join(json.loads(old_row[1]))
            except Exception:
                old_tags = ""

        diaries_text = "\n".join(f"- {d[:200]}" for d in (new_diaries or [])[-5:])
        facts_text = "\n".join(f"- {f[:200]}" for f in (new_facts or [])[-10:])

        if not diaries_text and not facts_text:
            return False

        prompt = self._prompt_inc.format(
            old_timestamp=old_ts,
            old_summary=old_summary,
            old_tags=old_tags or "（无标签）",
            new_diaries=diaries_text or "（无新日记）",
            new_facts=facts_text or "（无新事实）",
        )

        try:
            result = await self.llm.chat("", prompt)
            if not result or not result.strip():
                return False

            # 应用增量 diff
            await self._apply_delta(uid, old_summary, result.strip())

            # 增量超阈值 → 全量重建压缩
            try:
                row = await self.atom_store.fetchone("""
                    SELECT incremental_count, diary_count_since_full
                    FROM user_persona WHERE uid=?
                """, (uid,))
                if row:
                    inc_cnt = row[0] or 0
                    dia_cnt = row[1] or 0
                    if inc_cnt >= 10 or dia_cnt >= 50:
                        logger.info(f"[Memory] 增量超阈值 ({inc_cnt}次/{dia_cnt}篇) → 全量重建压缩 {uid}")
                        self._cache.pop(uid, None)
                        asyncio.ensure_future(self.full_rebuild(uid))
            except Exception:
                pass

            return True
        except Exception as e:
            logger.warning(f"[Memory] 画像增量更新失败 {uid}: {e}")
            return False

    async def _apply_delta(self, uid: str, old_summary: str, delta_json: str):
        """应用 LLM 返回的增量 diff"""
        import json
        try:
            from ..core.diary_helper import _extract_json
            data = _extract_json(delta_json)
            if not isinstance(data, dict):
                return

            new_summary = old_summary
            for item in data.get("delete", []):
                new_summary = new_summary.replace(str(item), "")
            for item in data.get("modify", []):
                old_text = str(item.get("old", ""))
                new_text = str(item.get("new", ""))
                if old_text:
                    new_summary = new_summary.replace(old_text, new_text)
            for item in data.get("add", []):
                new_summary += f"\n- {item}"

            new_summary = "\n".join(l for l in new_summary.split("\n") if l.strip())
            if not new_summary:
                new_summary = old_summary

            # 提取标签
            tags = data.get("tags", [])
            if isinstance(tags, list):
                tags = json.dumps([t for t in tags if isinstance(t, str)], ensure_ascii=False)
            else:
                tags = ""

            await self.atom_store.save_persona(uid, new_summary, incremental=True, tags=tags)
            self._cache.pop(uid, None)
        except Exception as e:
            logger.warning(f"[Memory] 应用增量 diff 失败: {e}")

    # ═══════════════════════════════════════════════════
    #  全量重建（手动，高成本）
    # ═══════════════════════════════════════════════════

    async def full_rebuild(self, uid: str, days: int = 90) -> str | None:
        """全量重建：从 L1+L2 重新生成画像"""
        recent_diaries = await self._get_recent_diaries_batch(uid, count=10)
        recent_atoms = await self.atom_store.get_by_user(uid)
        recent_atoms = [a for a in recent_atoms if a.importance > 0.3][:30]
        atoms_text = "\n".join(
            f"- [{a.atom_type.value}] {a.content} (重要度:{a.importance})"
            for a in recent_atoms
        )

        user_prompt = (
            f"最近的日记：\n{recent_diaries}\n\n"
            f"最近的记忆原子：\n{atoms_text}\n"
        )

        try:
            new_persona = await self.llm.chat(self._prompt_full, user_prompt)
            if new_persona and new_persona.strip():
                await self.atom_store.save_persona(
                    uid, new_persona.strip(), new_persona.strip(), incremental=False
                )
                self._cache.pop(uid, None)
                return new_persona.strip()
        except Exception as e:
            logger.warning(f"[Memory] 全量重建画像失败 {uid}: {e}")
        return None

    # ═══════════════════════════════════════════════════
    #  工具
    # ═══════════════════════════════════════════════════

    async def _get_recent_diaries_batch(self, user_id: str, count: int = 5) -> str:
        rows = await self.diary_store.fetch("""
            SELECT date, content FROM diary_entries
            WHERE user_id = ? ORDER BY date DESC LIMIT ?
        """, (user_id, count))
        if not rows:
            return ""
        entries = []
        for r in rows:
            date_str, content = r[0], (r[1] or "")
            if content.startswith("---"):
                end = content.find("\n---", 3)
                if end != -1:
                    content = content[end + 5:].strip()
            entries.append(f"--- {date_str} ---\n{content[:500]}")
        return "\n\n".join(entries)

    async def invalidate_cache(self, uid: str):
        self._cache.pop(uid, None)
