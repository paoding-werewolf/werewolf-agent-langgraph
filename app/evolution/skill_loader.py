"""evolution/skill_loader.py — 渐进式策略加载器（MySQL 持久化）

三层加载：
  Layer 1 (始终): 策略名+描述索引
  Layer 2 (对局开始时): 当前角色+当前阶段相关的 1-3 个策略全文
  Layer 3 (反思时): 按需加载非默认版本，用于对比
"""
import logging
import random
import asyncio
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone

from sqlalchemy import func

from evolution.config import EvolutionConfig
from evolution.db import get_session
from evolution.models import EvolutionSkill, EvolutionSkillVersion
from evolution.conjugate_agent import maybe_create_conjugate_agent, generate_lore_async

logger = logging.getLogger("evolution.skill_loader")


class SkillLoader:
    def __init__(self, cfg: EvolutionConfig):
        self.cfg = cfg

    def _strategy_role(self, role: str) -> str:
        return "wolf" if role in {"wolf", "wolf_king"} else role

    # ── Layer 1: 索引 ──────────────────────────────────────

    def load_index(self) -> List[Dict[str, str]]:
        """加载全局策略索引（第 1 层），始终注入 system prompt。"""
        session = get_session()
        try:
            skills = session.query(EvolutionSkill).all()
            return [
                {
                    "name": s.skill_name,
                    "description": s.description or "",
                    "role": s.role,
                    "current_version": s.current_default.lstrip("v"),
                    "tags": s.tags_json or [],
                }
                for s in skills
            ]
        finally:
            session.close()

    def format_index_for_prompt(self, my_role: str) -> str:
        """将索引格式化为 prompt 片段。只展示当前角色 + common 类别。"""
        skills = self.load_index()
        strategy_role = self._strategy_role(my_role)
        relevant = [s for s in skills if s.get("role") in (strategy_role, "common")]
        if not relevant:
            return ""
        lines = ["## Available Strategy Skills"]
        for s in relevant:
            lines.append(f"- **{s['name']}** (v{s['current_version']}): {s['description']}")
        return "\n".join(lines)

    # ── Layer 2: 全文加载 ────────────────────────────────────

    def load_skill_full(self, skill_name: str, version: Optional[str] = None) -> Optional[str]:
        """加载指定策略的完整 Markdown 内容（第 2 层）。"""
        session = get_session()
        try:
            skill = session.query(EvolutionSkill).filter_by(skill_name=skill_name).first()
            if not skill:
                return None

            if version is None:
                version = skill.current_default

            v = session.query(EvolutionSkillVersion).filter_by(
                skill_id=skill.id, version=version
            ).first()
            return v.content_markdown if v else None
        finally:
            session.close()

    def load_skills_for_context(self, my_role: str, phase: str) -> str:
        """根据当前角色和阶段，加载相关策略全文。最多加载 3 个。"""
        skills = self.load_index()
        strategy_role = self._strategy_role(my_role)
        relevant = [
            s for s in skills
            if s.get("role") in (strategy_role, "common")
        ]

        phase_tags = self._phase_to_tags(phase)
        scored = []
        for s in relevant:
            overlap = len(set(s.get("tags", [])) & phase_tags)
            scored.append((overlap, s))
        scored.sort(key=lambda x: -x[0])

        loaded_parts = []
        for _, s in scored[:3]:
            content = self.load_skill_full(s["name"])
            if content:
                loaded_parts.append(f"### Strategy: {s['name']}\n{content}")

        return "\n\n".join(loaded_parts)

    def load_skills_with_versions(self, my_role: str, phase: str,
                                  versions_used: dict) -> str:
        """与 load_skills_for_context 相同，但按 versions_used 指定版本加载。"""
        skills = self.load_index()
        strategy_role = self._strategy_role(my_role)
        relevant = [
            s for s in skills
            if s.get("role") in (strategy_role, "common")
        ]

        phase_tags = self._phase_to_tags(phase)
        scored = []
        for s in relevant:
            overlap = len(set(s.get("tags", [])) & phase_tags)
            scored.append((overlap, s))
        scored.sort(key=lambda x: -x[0])

        loaded_parts = []
        for _, s in scored[:3]:
            version = versions_used.get(s["name"])
            content = self.load_skill_full(s["name"], version=version)
            if content:
                loaded_parts.append(f"### Strategy: {s['name']}\n{content}")

        return "\n\n".join(loaded_parts)

    # ── 版本竞争相关 ─────────────────────────────────────────

    def get_version_for_game(self, skill_name: str) -> str:
        """版本竞争：决定本局使用哪个版本。

        如果策略级已有足够对局数据（skill_games_played >= warmup_games），
        说明策略整体经验充足，candidate 版本直接按固定比例获得对局机会。
        否则（策略还很新），沿用 warmup 逻辑给 candidate 机会。
        """
        session = get_session()
        try:
            skill = session.query(EvolutionSkill).filter_by(skill_name=skill_name).first()
            if not skill:
                return "v1"

            current = skill.current_default
            versions = session.query(EvolutionSkillVersion).filter_by(skill_id=skill.id).all()

            for v in versions:
                if v.status == "candidate":
                    if skill.skill_games_played >= self.cfg.versioning.warmup_games:
                        # 策略已成熟：candidate 按固定比例获得对局，不受自身 games_played 限制
                        if random.random() < self.cfg.versioning.warmup_allocation:
                            return v.version
                    else:
                        # 策略还很新：沿用旧逻辑，candidate 需要自身还没攒够 warmup 场次
                        if v.games_played < self.cfg.versioning.warmup_games:
                            if random.random() < self.cfg.versioning.warmup_allocation:
                                return v.version

            return current
        finally:
            session.close()

    def record_version_usage(self, skill_name: str, version: str, won: bool):
        """对局结束后记录版本使用情况（版本级 + 策略级双写）。"""
        session = get_session()
        try:
            skill = session.query(EvolutionSkill).filter_by(skill_name=skill_name).first()
            if not skill:
                return

            v = session.query(EvolutionSkillVersion).filter_by(
                skill_id=skill.id, version=version
            ).first()
            if not v:
                return

            # 版本级计数
            v.games_played += 1
            if won:
                v.wins += 1
            v.win_rate = v.wins / v.games_played
            v.last_used_at = datetime.now(timezone.utc).replace(tzinfo=None)

            # 策略级计数
            skill.skill_games_played += 1
            if won:
                skill.skill_wins += 1
            skill.skill_win_rate = skill.skill_wins / skill.skill_games_played

            lore_agent_id = None
            if v.status == "candidate":
                lore_agent_id = self._check_promotion(session, skill, v)

            session.commit()
            if lore_agent_id:
                self._schedule_lore_generation(lore_agent_id)
            logger.debug(f"Version usage recorded: {skill_name} {version}, games={v.games_played}, wins={v.wins}, win_rate={v.win_rate:.2f}")
            logger.debug(f"Skill usage: {skill_name}, total_games={skill.skill_games_played}, total_wins={skill.skill_wins}, total_win_rate={skill.skill_win_rate:.2f}")
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _check_promotion(self, session, skill: EvolutionSkill, candidate: EvolutionSkillVersion) -> int | None:
        """检查 candidate 版本是否满足升级条件。"""
        current_default = skill.current_default
        current = session.query(EvolutionSkillVersion).filter_by(
            skill_id=skill.id, version=current_default
        ).first()

        cfg = self.cfg.versioning
        if current:
            if (candidate.games_played >= cfg.promotion_min_games and
                    candidate.win_rate - current.win_rate >= cfg.promotion_min_win_rate_delta):
                previous_version = current_default
                candidate.status = "active"
                skill.current_default = candidate.version
                current.status = "superseded"
                logger.info(f"Version PROMOTED: {skill.skill_name} {candidate.version} (games={candidate.games_played}, win_rate={candidate.win_rate:.2f}) superseded {current_default} (win_rate={current.win_rate:.2f})")
                agent = maybe_create_conjugate_agent(
                    session,
                    trigger_skill=skill,
                    previous_version=previous_version,
                    promoted_version=candidate,
                )
                if agent:
                    return agent.id
        return None

    def _schedule_lore_generation(self, agent_id: int) -> None:
        """尽力后台生成 lore；无事件循环时不阻塞晋升事务。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.info("Skip async lore generation for agent %s: no running event loop", agent_id)
            return
        loop.create_task(generate_lore_async(agent_id))

    def create_new_version(self, skill_name: str, content: str,
                           source: str = "debounced_update",
                           trigger_cluster: str = "") -> str:
        """创建策略新版本，返回版本号（如 "v3"）。"""
        session = get_session()
        try:
            skill = session.query(EvolutionSkill).filter_by(skill_name=skill_name).first()
            if not skill:
                role = skill_name.split("-", 1)[0] if "-" in skill_name else "common"
                skill = EvolutionSkill(
                    skill_name=skill_name,
                    role=role,
                    description="",
                    tags_json=[],
                    current_default="v1",
                )
                session.add(skill)
                session.flush()
                logger.info(f"Created new skill entry: {skill_name} (role={role})")

            existing_versions = session.query(EvolutionSkillVersion).filter_by(
                skill_id=skill.id
            ).order_by(EvolutionSkillVersion.created_at.asc()).all()

            next_number = max(
                (int(v.version.lstrip("v")) for v in existing_versions if v.version.startswith("v")),
                default=0
            ) + 1
            version_name = f"v{next_number}"

            version = EvolutionSkillVersion(
                skill_id=skill.id,
                version=version_name,
                status="active" if skill.current_default == version_name else "candidate",
                source=source,
                trigger_cluster_id=trigger_cluster or None,
                pinned=False,
                content_markdown=content,
                games_played=0,
                wins=0,
                win_rate=0.0,
                last_used_at=None,
            )
            session.add(version)
            session.flush()

            # If current_default points to a nonexistent version (ghost default),
            # or this is the first version, set it as the new default and mark active
            if not existing_versions or not session.query(EvolutionSkillVersion).filter_by(
                skill_id=skill.id, version=skill.current_default
            ).first():
                skill.current_default = version_name
                version.status = "active"

            max_v = self.cfg.versioning.max_versions_per_skill
            if len(existing_versions) + 1 > max_v:
                self._prune_old_versions(session, skill, max_v)

            session.commit()
            logger.info(f"Created new version: {skill_name} {version_name} (source={source}, cluster={trigger_cluster or 'N/A'})")
            return version_name
        except Exception:
            session.rollback()
            logger.exception(f"Failed to create new version for {skill_name}")
            raise
        finally:
            session.close()

    def _prune_old_versions(self, session, skill: EvolutionSkill, keep: int):
        """删除最旧的非 pinned 版本，保留 keep 个。"""
        versions = session.query(EvolutionSkillVersion).filter_by(skill_id=skill.id).all()
        deletable = [
            v for v in versions
            if not v.pinned and v.status in ("superseded", "archived")
        ]
        deletable.sort(key=lambda v: v.created_at or datetime.min)

        total = len(versions)
        for v in deletable:
            if total <= keep:
                break
            session.delete(v)
            total -= 1

    def rollback(self, skill_name: str, target_version: str) -> bool:
        """回退策略到指定版本。"""
        session = get_session()
        try:
            skill = session.query(EvolutionSkill).filter_by(skill_name=skill_name).first()
            if not skill:
                return False

            target = session.query(EvolutionSkillVersion).filter_by(
                skill_id=skill.id, version=target_version
            ).first()
            if not target:
                return False

            current = session.query(EvolutionSkillVersion).filter_by(
                skill_id=skill.id, version=skill.current_default
            ).first()
            if current and current.id != target.id:
                current.status = "superseded"

            target.status = "active"
            skill.current_default = target_version
            session.commit()
            return True
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def pin_version(self, skill_name: str, version: str, pinned: bool) -> bool:
        """固定或取消固定版本。"""
        session = get_session()
        try:
            skill = session.query(EvolutionSkill).filter_by(skill_name=skill_name).first()
            if not skill:
                return False

            v = session.query(EvolutionSkillVersion).filter_by(
                skill_id=skill.id, version=version
            ).first()
            if not v:
                return False

            v.pinned = pinned
            session.commit()
            return True
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def delete_version(self, skill_name: str, version: str) -> bool:
        """删除版本（不可删 pinned 或 current_default）。"""
        session = get_session()
        try:
            skill = session.query(EvolutionSkill).filter_by(skill_name=skill_name).first()
            if not skill or skill.current_default == version:
                return False

            v = session.query(EvolutionSkillVersion).filter_by(
                skill_id=skill.id, version=version
            ).first()
            if not v or v.pinned:
                return False

            session.delete(v)
            session.commit()
            return True
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def list_skills(self) -> List[Dict[str, Any]]:
        """列出所有策略及其版本。"""
        session = get_session()
        try:
            skills = session.query(EvolutionSkill).order_by(EvolutionSkill.skill_name.asc()).all()
            result = []
            for s in skills:
                versions = session.query(EvolutionSkillVersion).filter_by(
                    skill_id=s.id
                ).order_by(EvolutionSkillVersion.created_at.desc()).all()
                current = next((v for v in versions if v.version == s.current_default), None)
                result.append({
                    "skill_name": s.skill_name,
                    "role": s.role,
                    "description": s.description or "",
                    "current_default": s.current_default,
                    "tags": s.tags_json or [],
                    "skill_games_played": s.skill_games_played,
                    "skill_wins": s.skill_wins,
                    "skill_win_rate": float(s.skill_win_rate or 0.0),
                    "versions": [self._serialize_version(v) for v in versions],
                    "current_content": current.content_markdown if current else None,
                })
            return result
        finally:
            session.close()

    def get_skill_detail(self, skill_name: str) -> Optional[Dict[str, Any]]:
        """获取单个策略详情。"""
        session = get_session()
        try:
            skill = session.query(EvolutionSkill).filter_by(skill_name=skill_name).first()
            if not skill:
                return None
            versions = session.query(EvolutionSkillVersion).filter_by(
                skill_id=skill.id
            ).order_by(EvolutionSkillVersion.created_at.desc()).all()
            current = next((v for v in versions if v.version == skill.current_default), None)
            return {
                "skill_name": skill.skill_name,
                "role": skill.role,
                "description": skill.description or "",
                "current_default": skill.current_default,
                "tags": skill.tags_json or [],
                "skill_games_played": skill.skill_games_played,
                "skill_wins": skill.skill_wins,
                "skill_win_rate": float(skill.skill_win_rate or 0.0),
                "versions": [self._serialize_version(v) for v in versions],
                "current_content": current.content_markdown if current else None,
            }
        finally:
            session.close()

    def _load_versions_meta(self, skill_name: str) -> Dict[str, Any]:
        """为 provider/agents 暴露版本元数据。"""
        session = get_session()
        try:
            skill = session.query(EvolutionSkill).filter_by(skill_name=skill_name).first()
            if not skill:
                return {"current_default": None, "versions": {}}

            versions = session.query(EvolutionSkillVersion).filter_by(
                skill_id=skill.id
            ).order_by(EvolutionSkillVersion.created_at.desc()).all()
            return {
                "current_default": skill.current_default,
                "versions": {
                    v.version: self._serialize_version(v)
                    for v in versions
                },
            }
        finally:
            session.close()

    def get_version_content(self, skill_name: str, version: str) -> Optional[str]:
        """获取指定版本 Markdown 内容。"""
        session = get_session()
        try:
            skill = session.query(EvolutionSkill).filter_by(skill_name=skill_name).first()
            if not skill:
                return None
            v = session.query(EvolutionSkillVersion).filter_by(
                skill_id=skill.id, version=version
            ).first()
            return v.content_markdown if v else None
        finally:
            session.close()

    def diff_versions(self, skill_name: str, version_a: str, version_b: str) -> Optional[Dict[str, str]]:
        """获取两个版本的 diff 数据。"""
        session = get_session()
        try:
            skill = session.query(EvolutionSkill).filter_by(skill_name=skill_name).first()
            if not skill:
                return None
            va = session.query(EvolutionSkillVersion).filter_by(
                skill_id=skill.id, version=version_a
            ).first()
            vb = session.query(EvolutionSkillVersion).filter_by(
                skill_id=skill.id, version=version_b
            ).first()
            if not va or not vb:
                return None
            return {
                "version_a": version_a,
                "content_a": va.content_markdown,
                "version_b": version_b,
                "content_b": vb.content_markdown,
            }
        finally:
            session.close()

    def _serialize_version(self, v: EvolutionSkillVersion) -> Dict[str, Any]:
        return {
            "version": v.version,
            "status": v.status,
            "source": v.source,
            "pinned": v.pinned,
            "created_at": v.created_at.isoformat() if v.created_at else "",
            "games_played": v.games_played,
            "wins": v.wins,
            "win_rate": float(v.win_rate or 0.0),
            "last_used": v.last_used_at.isoformat() if v.last_used_at else None,
        }

    def _phase_to_tags(self, phase: str) -> set:
        """将游戏阶段映射为策略标签集合。"""
        PHASE_TAG_MAP = {
            "seer_check": {"check", "night", "verify"},
            "wolf_kill": {"kill", "night", "wolf-strategy"},
            "witch_action": {"potion", "night", "heal", "poison"},
            "guard_action": {"protect", "night", "guard"},
            "election": {"sheriff", "election", "campaign"},
            "discussion": {"speech", "analysis", "bluff"},
            "vote": {"vote", "elimination", "strategy"},
        }
        return PHASE_TAG_MAP.get(phase, {"general"})
