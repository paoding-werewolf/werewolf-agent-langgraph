"""evolution/curator.py — 自主策展人（MySQL 持久化）

两阶段策略库维护：
  阶段一：确定性状态转移（active → stale → archived）
  阶段二：LLM 审查（keep / patch / consolidate / archive）
"""
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional
import logging

from evolution.config import EvolutionConfig
from evolution.db import get_session
from evolution.models import EvolutionSkill, EvolutionSkillVersion, EvolutionRuntimeState
from evolution.skill_loader import SkillLoader
from agents.llm_caller import LLMCaller

logger = logging.getLogger(__name__)


class Curator:
    def __init__(self, cfg: EvolutionConfig):
        self.cfg = cfg
        self.loader = SkillLoader(cfg)

    def should_run(self, is_game_in_progress: bool = False) -> bool:
        """判断是否应该触发 Curator。"""
        if is_game_in_progress:
            return False
        if not self.cfg.curator.enabled:
            return False

        state = self._load_state()
        last_run = state.get("last_run_at")

        if not last_run:
            self._save_state({"last_run_at": datetime.now(timezone.utc).isoformat()})
            return False

        now = datetime.now(timezone.utc)
        last_run_dt = datetime.fromisoformat(last_run)
        hours_since_run = (now - last_run_dt).total_seconds() / 3600

        if hours_since_run < self.cfg.curator.interval_hours:
            return False

        last_game_end = state.get("last_game_end_at")
        if last_game_end:
            hours_since_game = (now - datetime.fromisoformat(last_game_end)).total_seconds() / 3600
            if hours_since_game < self.cfg.curator.min_idle_hours:
                return False

        return True

    def _current_counts(self) -> tuple:
        """返回当前 (confirmed_count, total_games)。"""
        from evolution.models import EvolutionBufferItem, EvolutionGameArchive
        from sqlalchemy import func
        session = get_session()
        try:
            confirmed = session.query(func.count(EvolutionBufferItem.id)).filter_by(
                item_type="confirmed").scalar() or 0
            games = session.query(func.count(EvolutionGameArchive.id)).scalar() or 0
            return confirmed, games
        finally:
            session.close()

    def run(self) -> Dict:
        """执行 Curator 审查。返回操作摘要。"""
        summary = {"phase1": {}, "phase2": {}}

        summary["phase1"] = self._phase1_state_transitions()
        summary["phase2"] = self._phase2_llm_review()

        confirmed, games = self._current_counts()
        self._save_state({
            "last_run_at": datetime.now(timezone.utc).isoformat(),
            "last_confirmed_count": confirmed,
            "last_total_games": games,
        })

        return summary

    def _phase1_state_transitions(self) -> Dict:
        """确定性状态转移：active → stale → archived。"""
        result = {"staled": [], "archived": []}
        cfg = self.cfg.versioning
        now = datetime.now(timezone.utc)

        session = get_session()
        try:
            versions = session.query(EvolutionSkillVersion).all()
            for v in versions:
                if v.pinned:
                    continue

                last_used = v.last_used_at or v.created_at
                if not last_used:
                    continue

                if last_used.tzinfo is None:
                    last_used = last_used.replace(tzinfo=timezone.utc)

                days_since = (now - last_used).days

                if v.status == "active" and days_since >= cfg.demotion_stale_days:
                    v.status = "stale"
                    skill = session.query(EvolutionSkill).filter_by(id=v.skill_id).first()
                    skill_name = skill.skill_name if skill else str(v.skill_id)
                    result["staled"].append(f"{skill_name}/{v.version}")

                elif v.status == "stale" and days_since >= cfg.demotion_archive_days:
                    v.status = "archived"
                    skill = session.query(EvolutionSkill).filter_by(id=v.skill_id).first()
                    skill_name = skill.skill_name if skill else str(v.skill_id)
                    result["archived"].append(f"{skill_name}/{v.version}")

            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        return result

    def _phase2_llm_review(self) -> Dict:
        """LLM 审查：对每个非 pinned、非 bundled 策略做 keep/patch/consolidate/archive 判定。"""
        result = {"reviewed": 0, "kept": 0, "patched": 0, "consolidated": 0, "archived": 0}

        review_llm = LLMCaller()
        review_llm.model = self.cfg.clustering_model

        skills_reviewed = 0
        session = get_session()
        try:
            skills = session.query(EvolutionSkill).all()
            for skill in skills:
                if skills_reviewed >= self.cfg.curator.max_iterations:
                    break

                current_v = session.query(EvolutionSkillVersion).filter_by(
                    skill_id=skill.id
                ).order_by(EvolutionSkillVersion.id.desc()).first()
                if not current_v:
                    continue

                if current_v.pinned or current_v.source == "bundled":
                    continue

                content = current_v.content_markdown
                usage = {
                    "games_played": current_v.games_played,
                    "wins": current_v.wins,
                    "win_rate": float(current_v.win_rate or 0),
                    "last_used": current_v.last_used_at.isoformat() if current_v.last_used_at else "N/A",
                }

                decision = self._llm_review_skill(content, usage, review_llm)
                result["reviewed"] += 1
                skills_reviewed += 1

                if decision == "keep":
                    result["kept"] += 1
                elif decision == "patch":
                    patched_content = self._llm_patch_skill(content, usage, review_llm)
                    if patched_content:
                        current_v.content_markdown = patched_content
                        result["patched"] += 1
                    else:
                        result["kept"] += 1
                elif decision == "consolidate":
                    consolidated = self._llm_consolidate_skill(
                        skill.skill_name, content, review_llm
                    )
                    if consolidated:
                        self.loader.create_new_version(
                            skill_name=skill.skill_name,
                            content=consolidated["content"],
                            source="curator_consolidation",
                            trigger_cluster=consolidated.get("merged_with", ""),
                        )
                        current_v.status = "archived"
                        result["consolidated"] += 1
                    else:
                        result["kept"] += 1
                elif decision == "archive":
                    current_v.status = "archived"
                    result["archived"] += 1

            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        return result

    def _llm_review_skill(self, content: str, usage: Dict, llm: LLMCaller) -> str:
        """LLM 审查单个策略，返回 keep/patch/consolidate/archive。"""
        prompt = f"""审查以下狼人杀策略文档，做出维护决策。

策略内容：
{content[:3000]}

使用数据：
- 对局数: {usage.get('games_played', 0)}
- 胜率: {usage.get('win_rate', 0):.2f}
- 最后使用: {usage.get('last_used', 'N/A')}

判定标准：
- keep: 策略质量良好，数据支持
- patch: 有小瑕疵需要修补
- consolidate: 与其他策略重叠，应合并
- archive: 质量不足或完全被新版本替代

只回答 keep / patch / consolidate / archive 之一。"""

        try:
            resp = llm.client.chat.completions.create(
                model=llm.model,
                messages=[
                    {"role": "system", "content": "You are a strategy library curator."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                max_tokens=500,
            )
            answer = (resp.choices[0].message.content or "").strip().lower()
            for decision in ["keep", "patch", "consolidate", "archive"]:
                if decision in answer:
                    return decision
        except Exception:
            logger.exception("LLM review call failed, defaulting to keep")
        return "keep"

    def _llm_patch_skill(self, content: str, usage: Dict, llm: LLMCaller) -> Optional[str]:
        """LLM 修补策略文档的瑕疵，返回修补后的完整文档。失败返回 None。"""
        prompt = f"""修补以下狼人杀策略文档中的瑕疵。

策略内容：
{content[:3000]}

使用数据：
- 对局数: {usage.get('games_played', 0)}
- 胜率: {usage.get('win_rate', 0):.2f}

要求：
1. 修正明显的逻辑矛盾或表述不清
2. 保留原有策略的核心思路
3. 保持原有的 Markdown + YAML frontmatter 格式
4. 输出修补后的完整文档"""

        try:
            resp = llm.client.chat.completions.create(
                model=llm.model,
                messages=[
                    {"role": "system", "content": "你是狼人杀策略文档修补专家。输出完整的修补后文档。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
            )
            result = (resp.choices[0].message.content or "").strip()
            if len(result) > 50 and "---" in result:
                return result
        except Exception:
            pass
        return None

    def _llm_consolidate_skill(self, skill_name: str, content: str,
                                llm: LLMCaller) -> Optional[Dict]:
        """LLM 将当前策略与重叠策略合并。返回 {"content": str, "merged_with": str} 或 None。"""
        session = get_session()
        try:
            skill = session.query(EvolutionSkill).filter_by(skill_name=skill_name).first()
            if not skill:
                return None

            siblings = session.query(EvolutionSkill).filter_by(role=skill.role).all()
            sibling_skills = []
            for s in siblings:
                if s.id == skill.id:
                    continue
                current_v = session.query(EvolutionSkillVersion).filter_by(
                    skill_id=s.id, version=s.current_default
                ).first()
                if current_v and current_v.content_markdown:
                    sibling_skills.append({
                        "name": s.skill_name,
                        "content": current_v.content_markdown[:1500],
                    })

            if not sibling_skills:
                return None

            siblings_text = "\n\n".join(
                f"### 策略: {s['name']}\n{s['content']}" for s in sibling_skills[:3]
            )

            prompt = f"""判断以下策略是否与相邻策略有显著重叠，如果有则合并。

当前策略 ({skill_name})：
{content[:2000]}

相邻策略：
{siblings_text}

如果存在显著重叠（覆盖相似决策空间），输出合并后的完整策略文档（Markdown + YAML frontmatter）。
如果没有显著重叠，只回答 "no_merge"。"""

            try:
                resp = llm.client.chat.completions.create(
                    model=llm.model,
                    messages=[
                        {"role": "system", "content": "你是策略库策展专家。判断重叠并合并。"},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.2,
                )
                result = (resp.choices[0].message.content or "").strip()
                if "no_merge" in result.lower():
                    return None
                if len(result) > 50 and "---" in result:
                    return {
                        "content": result,
                        "merged_with": ", ".join(s["name"] for s in sibling_skills[:3]),
                    }
            except Exception:
                pass
            return None
        finally:
            session.close()

    def _load_state(self) -> Dict:
        session = get_session()
        try:
            record = session.get(EvolutionRuntimeState, "curator")
            return dict(record.payload_json) if record else {}
        finally:
            session.close()

    def _save_state(self, updates: Dict):
        """合并写入状态（保留未提及的字段）。"""
        from sqlalchemy.orm.attributes import flag_modified

        now = datetime.now(timezone.utc).replace(tzinfo=None)

        session = get_session()
        try:
            record = session.get(EvolutionRuntimeState, "curator")
            if record:
                merged = dict(record.payload_json)
                merged.update(updates)
                record.payload_json = merged
                flag_modified(record, "payload_json")
                record.updated_at = now
            else:
                session.add(EvolutionRuntimeState(state_key="curator", payload_json=updates))
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
