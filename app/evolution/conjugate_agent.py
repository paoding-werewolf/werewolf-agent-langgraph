"""共轭 Agent 快照管理。

一个 ConjugateAgent 表示所有 skill current_default 的全局快照。只有最新快照
参与 warmup、反思和版本竞争；旧快照只作为可参赛的冻结版本。
"""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import desc

from evolution.db import get_session
from evolution.models import ConjugateAgent, EvolutionSkill, EvolutionSkillVersion

logger = logging.getLogger("evolution.conjugate_agent")


_NAME_PREFIXES = [
    "影刃",
    "霜牙",
    "烬瞳",
    "夜弦",
    "银誓",
    "玄棘",
    "雾裁",
    "星狩",
    "赤冕",
    "寒鸦",
]
_EPOCH_NAMES = ["初纪", "二纪", "三纪", "四纪", "五纪", "六纪", "七纪", "八纪", "九纪", "十纪"]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def snapshot_all_skill_versions(session) -> dict[str, str]:
    """返回全局 current_default 快照，按 skill_name 稳定排序。"""
    rows = (
        session.query(EvolutionSkill)
        .order_by(EvolutionSkill.skill_name.asc())
        .all()
    )
    return {row.skill_name: row.current_default for row in rows}


def compute_global_fingerprint(session) -> str:
    """根据全局快照生成稳定指纹。相同组合必然得到相同 fingerprint。"""
    snapshot = snapshot_all_skill_versions(session)
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_latest_conjugate_agent(session) -> ConjugateAgent | None:
    return session.query(ConjugateAgent).order_by(desc(ConjugateAgent.id)).first()


def get_conjugate_agent(session, external_agent_id: str) -> ConjugateAgent | None:
    agent_id = parse_conjugate_agent_id(external_agent_id)
    if agent_id is None:
        return None
    return session.get(ConjugateAgent, agent_id)


def parse_conjugate_agent_id(external_agent_id: str | None) -> int | None:
    if not external_agent_id:
        return None
    parts = str(external_agent_id).split(":")
    if len(parts) != 2 or parts[0] != "agent":
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def is_latest_conjugate(external_agent_id: str | None) -> bool:
    """判断本次对局是否属于可进化的 Default Agent。"""
    if not external_agent_id or external_agent_id == "default:common":
        return True

    session = get_session()
    try:
        latest = get_latest_conjugate_agent(session)
        agent_id = parse_conjugate_agent_id(external_agent_id)
        return latest is not None and agent_id == latest.id
    finally:
        session.close()


def ensure_initial_conjugate_agent(session) -> ConjugateAgent | None:
    """在已有 skill 但无共轭快照时创建初代 Agent。"""
    existing = get_latest_conjugate_agent(session)
    if existing:
        return existing

    skill_count = session.query(EvolutionSkill).count()
    if skill_count <= 0:
        return None

    first_version = (
        session.query(EvolutionSkillVersion)
        .order_by(EvolutionSkillVersion.created_at.asc())
        .first()
    )
    born_at = first_version.created_at if first_version and first_version.created_at else _utc_now()
    snapshot = snapshot_all_skill_versions(session)
    fingerprint = compute_global_fingerprint(session)
    agent = ConjugateAgent(
        fingerprint=fingerprint,
        agent_name=generate_agent_name(None),
        avatar_seed=generate_avatar_seed(fingerprint),
        born_at=born_at,
        skill_versions_json=snapshot,
        changelog="Genesis: initial skill library snapshot",
        lore="",
    )
    session.add(agent)
    session.flush()
    return agent


def list_conjugate_agents() -> list[ConjugateAgent]:
    session = get_session()
    try:
        ensure_initial_conjugate_agent(session)
        session.commit()
        return (
            session.query(ConjugateAgent)
            .order_by(desc(ConjugateAgent.id))
            .all()
        )
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def maybe_create_conjugate_agent(
    session,
    trigger_skill: EvolutionSkill,
    previous_version: str,
    promoted_version: EvolutionSkillVersion,
) -> ConjugateAgent | None:
    """current_default 变化后检测指纹，必要时创建新的共轭 Agent。"""
    fingerprint = compute_global_fingerprint(session)
    latest = get_latest_conjugate_agent(session)
    if latest and latest.fingerprint == fingerprint:
        return None

    snapshot = snapshot_all_skill_versions(session)
    changelog = generate_changelog(
        session,
        latest,
        trigger_skill.skill_name,
        previous_version,
        promoted_version.version,
    )
    agent = ConjugateAgent(
        fingerprint=fingerprint,
        agent_name=generate_agent_name(latest),
        avatar_seed=generate_avatar_seed(fingerprint),
        born_at=promoted_version.created_at or _utc_now(),
        skill_versions_json=snapshot,
        changelog=changelog,
        lore="",
    )
    session.add(agent)
    session.flush()
    logger.info(
        "Conjugate agent born: id=%s name=%s fingerprint=%s trigger=%s %s->%s",
        agent.id,
        agent.agent_name,
        agent.fingerprint[:16],
        trigger_skill.skill_name,
        previous_version,
        promoted_version.version,
    )
    return agent


def generate_agent_name(previous: ConjugateAgent | None) -> str:
    next_index = 1 if previous is None else previous.id + 1
    prefix = _NAME_PREFIXES[(next_index - 1) % len(_NAME_PREFIXES)]
    epoch = _EPOCH_NAMES[(next_index - 1) % len(_EPOCH_NAMES)]
    cycle = (next_index - 1) // len(_EPOCH_NAMES)
    return f"{prefix}-{epoch}" if cycle == 0 else f"{prefix}-{epoch}-{cycle + 1}"


def generate_avatar_seed(fingerprint: str) -> str:
    return f"conjugate-{fingerprint[:24]}"


def generate_changelog(
    session,
    previous_agent: ConjugateAgent | None,
    trigger_skill_name: str,
    previous_version: str,
    new_version: str,
) -> str:
    skill = session.query(EvolutionSkill).filter_by(skill_name=trigger_skill_name).first()
    old_content = _load_version_content(session, skill, previous_version) if skill else ""
    new_content = _load_version_content(session, skill, new_version) if skill else ""
    diff_summary = _summarize_markdown_diff(old_content, new_content)
    previous_id = previous_agent.id if previous_agent else 0

    lines = [
        f"## Agent #{previous_id + 1} Changelog",
        f"- **{trigger_skill_name}**: {previous_version} -> {new_version}",
    ]
    lines.extend(f"  - {item}" for item in diff_summary)
    return "\n".join(lines)


def _load_version_content(session, skill: EvolutionSkill | None, version: str) -> str:
    if not skill or not version:
        return ""
    row = (
        session.query(EvolutionSkillVersion)
        .filter_by(skill_id=skill.id, version=version)
        .first()
    )
    return row.content_markdown if row else ""


def _summarize_markdown_diff(old_content: str, new_content: str) -> list[str]:
    old_lines = _meaningful_lines(old_content)
    new_lines = _meaningful_lines(new_content)
    diff = list(difflib.ndiff(old_lines, new_lines))
    added = [line[2:] for line in diff if line.startswith("+ ")]
    removed = [line[2:] for line in diff if line.startswith("- ")]

    result: list[str] = []
    result.extend(_format_diff_items("Added", added[:3]))
    result.extend(_format_diff_items("Removed", removed[:2]))
    if not result:
        result.append("Modified: 策略文本发生更新，未检测到可独立摘录的段落级变化")
    return result[:5]


def _meaningful_lines(content: str) -> list[str]:
    return [
        line.strip()
        for line in (content or "").splitlines()
        if line.strip() and not line.strip().startswith("---")
    ]


def _format_diff_items(label: str, lines: Iterable[str]) -> list[str]:
    return [f'{label}: "{_truncate(line, 72)}"' for line in lines if line]


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else f"{text[:limit - 3]}..."


async def generate_lore_async(agent_id: int) -> None:
    """异步补写 lore。失败只记录日志，不影响晋升事务。"""
    try:
        await asyncio.to_thread(_generate_lore_sync, agent_id)
    except Exception:
        logger.exception("Failed to generate lore for conjugate agent %s", agent_id)


def _generate_lore_sync(agent_id: int) -> None:
    from agents.llm_caller import llm

    session = get_session()
    try:
        agent = session.get(ConjugateAgent, agent_id)
        if not agent or agent.lore:
            return
        previous = (
            session.query(ConjugateAgent)
            .filter(ConjugateAgent.id < agent.id)
            .order_by(desc(ConjugateAgent.id))
            .first()
        )
        roles = _roles_for_snapshot(session, agent.skill_versions_json or {})
        prompt = (
            "你是一位 3A 游戏的角色设定编剧。\n\n"
            f"前代角色设定：\n{previous.lore if previous and previous.lore else '无'}\n\n"
            f"本次进化变更：\n{agent.changelog}\n\n"
            f"新角色名：{agent.agent_name}\n"
            f"覆盖角色：{', '.join(roles) if roles else 'unknown'}\n\n"
            "请写一段角色介绍（200-300字），要求：\n"
            "1. 艺术性为主：讲述这个名字的起源、灵感、人格特质、命运轨迹\n"
            "2. 交叉引用技术变更：用隐喻或叙事手法暗示策略上的进化\n"
            "3. 与前代保持血脉联系：是进化/蜕变，不是完全重新诞生\n"
        )
        resp = llm.client.chat.completions.create(
            model=llm.model,
            messages=[
                {"role": "system", "content": "你只输出角色设定正文，不要解释写作过程。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.8,
        )
        lore = (resp.choices[0].message.content or "").strip()
        if not lore:
            return
        agent.lore = lore
        agent.updated_at = _utc_now()
        session.commit()
        logger.info("Lore generated for conjugate agent %s", agent_id)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _roles_for_snapshot(session, snapshot: dict[str, str]) -> list[str]:
    if not snapshot:
        return []
    rows = (
        session.query(EvolutionSkill.role)
        .filter(EvolutionSkill.skill_name.in_(list(snapshot.keys())))
        .distinct()
        .all()
    )
    return sorted(str(row[0]) for row in rows if row[0])
