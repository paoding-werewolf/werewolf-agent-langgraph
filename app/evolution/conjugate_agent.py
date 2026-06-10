"""共轭 Agent 快照管理。

一个 ConjugateAgent 表示所有 skill current_default 的全局快照。只有最新快照
参与 warmup、反思和版本竞争；旧快照只作为可参赛的冻结版本。
"""
from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import desc

from evolution.db import get_session
from evolution.models import ConjugateAgent, EvolutionSkill, EvolutionSkillVersion

logger = logging.getLogger("evolution.conjugate_agent")


_FALLBACK_NAME_ROOTS = [
    "影刃",
    "霜牙",
    "烬瞳",
    "夜弦",
    "银誓",
    "玄棘",
    "雾裁",
    "星狩",
    "赤冕",
    "幽衡",
    "澄镜",
    "断弦",
    "素问",
    "青垣",
    "黯烛",
    "孤衡",
]
_FALLBACK_NAME_MODIFIERS = ["玄", "霜", "烬", "夜", "银", "雾", "星", "赤", "幽", "澄", "青", "黯"]
_AGENT_NAME_RE = re.compile(r"^[\u4e00-\u9fff]{2,3}$")
_IDENTITY_RETRY_INITIAL_SECONDS = 1.0
_IDENTITY_RETRY_MAX_SECONDS = 300.0


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
    if str(external_agent_id) == "latest:evolution":
        session = get_session()
        try:
            latest = get_latest_conjugate_agent(session)
            return latest.id if latest else None
        finally:
            session.close()
    parts = str(external_agent_id).split(":")
    if len(parts) != 2 or parts[0] != "agent":
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def is_latest_conjugate(external_agent_id: str | None) -> bool:
    """判断本次对局是否属于可进化的 Default Agent。"""
    if not external_agent_id or external_agent_id in ("default:common", "latest:evolution"):
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

    born_at = _utc_now()
    snapshot = snapshot_all_skill_versions(session)
    fingerprint = compute_global_fingerprint(session)
    identity = generate_initial_agent_identity(session, fingerprint, snapshot, born_at)
    agent = ConjugateAgent(
        fingerprint=fingerprint,
        agent_name=identity["agent_name"],
        avatar_seed=generate_avatar_seed(fingerprint),
        born_at=born_at,
        skill_versions_json=snapshot,
        changelog=identity["changelog"],
        lore=identity["lore"],
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


def backfill_conjugate_agent_identities(session) -> int:
    """补齐旧 ConjugateAgent 的身份字段。"""
    agents = (
        session.query(ConjugateAgent)
        .order_by(ConjugateAgent.id.asc())
        .all()
    )
    updated = 0
    previous: ConjugateAgent | None = None
    for agent in agents:
        if _agent_identity_complete(agent):
            previous = agent
            continue

        facts = build_existing_agent_identity_facts(session, agent, previous)
        identity = generate_agent_identity(facts)
        if identity.get("agent_name"):
            agent.agent_name = identity["agent_name"]
        if identity.get("changelog"):
            agent.changelog = identity["changelog"]
        if identity.get("lore"):
            agent.lore = identity["lore"]
        agent.updated_at = _utc_now()
        updated += 1
        previous = agent
    return updated


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
    facts = build_evolution_facts(
        session,
        latest,
        trigger_skill.skill_name,
        previous_version,
        promoted_version.version,
        snapshot,
        _utc_now(),
    )
    identity = generate_agent_identity(facts)
    agent = ConjugateAgent(
        fingerprint=fingerprint,
        agent_name=identity["agent_name"],
        avatar_seed=generate_avatar_seed(fingerprint),
        born_at=_utc_now(),
        skill_versions_json=snapshot,
        changelog=identity["changelog"],
        lore=identity["lore"],
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


def _agent_identity_complete(agent: ConjugateAgent) -> bool:
    if not (agent.agent_name or "").strip():
        return False
    if not (agent.changelog or "").strip():
        return False
    if not (agent.lore or "").strip():
        return False
    return True


def agent_identity_needs_regeneration(agent: ConjugateAgent) -> bool:
    """判断共轭体身份是否需要重新生成。"""
    if not _agent_name_in_spec(agent.agent_name or ""):
        return True
    if not (agent.changelog or "").strip():
        return True
    if not (agent.lore or "").strip():
        return True
    return False


def fallback_agent_name(previous: ConjugateAgent | None) -> str:
    """生成符合身份规范的确定性兜底名：2-3 个中文字符，不带纪元/编号。"""
    next_index = _next_agent_index(previous)
    return _deterministic_agent_name(next_index)


def _next_agent_index(previous: ConjugateAgent | None) -> int:
    if previous is None:
        return 1
    try:
        previous_id = int(getattr(previous, "id", 0) or 0)
        return previous_id + 1 if previous_id > 0 else 1
    except (TypeError, ValueError):
        return 1


def _deterministic_agent_name(index: int) -> str:
    if index <= len(_FALLBACK_NAME_ROOTS):
        return _FALLBACK_NAME_ROOTS[index - 1]

    offset = index - len(_FALLBACK_NAME_ROOTS) - 1
    root = _FALLBACK_NAME_ROOTS[offset % len(_FALLBACK_NAME_ROOTS)]
    modifier = _FALLBACK_NAME_MODIFIERS[(offset // len(_FALLBACK_NAME_ROOTS)) % len(_FALLBACK_NAME_MODIFIERS)]
    if root.startswith(modifier):
        modifier = _FALLBACK_NAME_MODIFIERS[
            ((offset // len(_FALLBACK_NAME_ROOTS)) + 1) % len(_FALLBACK_NAME_MODIFIERS)
        ]
    return f"{modifier}{root}"


def generate_avatar_seed(fingerprint: str) -> str:
    return f"conjugate-{fingerprint[:24]}"


def generate_initial_agent_identity(
    session,
    fingerprint: str,
    snapshot: dict[str, str],
    born_at: datetime,
) -> dict[str, str]:
    roles = _roles_for_snapshot(session, snapshot)
    name = fallback_agent_name(None)
    changelog = "Genesis: initial skill library snapshot"
    facts = {
        "event_type": "genesis",
        "previous_agent_name": "无",
        "previous_born_at": "",
        "previous_lore": "",
        "existing_agent_names": _existing_agent_names(session),
        "trigger_skill_name": "initial-skill-library",
        "trigger_skill_role": ", ".join(roles) if roles else "unknown",
        "previous_version": "none",
        "new_version": "initial-snapshot",
        "born_at": born_at.isoformat() if born_at else _utc_now().isoformat(),
        "skill_versions_json": snapshot,
        "diff_summary": [
            f"Initial snapshot: {len(snapshot)} skills",
            f"Roles: {', '.join(roles) if roles else 'unknown'}",
            f"Fingerprint: {fingerprint[:16]}",
        ],
        "raw_diff": "",
        "fallback_agent_name": name,
        "fallback_changelog": changelog,
        "fallback_lore": (
            f"{name} 是共轭谱系的初代形态，携带 {len(snapshot)} 个策略快照"
            f"与 {', '.join(roles) if roles else 'unknown'} 角色覆盖。"
            "它还没有前代记忆，却已经把夜战、推理与伪装的基础本能封存在同一枚指纹中。"
        ),
    }
    return generate_agent_identity(facts)


def build_existing_agent_identity_facts(
    session,
    agent: ConjugateAgent,
    previous_agent: ConjugateAgent | None,
) -> dict[str, Any]:
    snapshot = dict(agent.skill_versions_json or {})
    roles = _roles_for_snapshot(session, snapshot)
    previous_snapshot = dict(previous_agent.skill_versions_json or {}) if previous_agent else {}
    changed = _changed_skill_versions(previous_snapshot, snapshot)
    is_genesis = previous_agent is None or not changed

    if is_genesis:
        return {
            "event_type": "genesis_backfill",
            "previous_agent_name": "无",
            "previous_born_at": "",
            "previous_lore": "",
            "existing_agent_names": [
                name for name in _existing_agent_names(session)
                if name != agent.agent_name
            ],
            "trigger_skill_name": "initial-skill-library",
            "trigger_skill_role": ", ".join(roles) if roles else "unknown",
            "previous_version": "none",
            "new_version": "initial-snapshot",
            "born_at": agent.born_at.isoformat() if agent.born_at else _utc_now().isoformat(),
            "skill_versions_json": snapshot,
            "diff_summary": [
                f"Initial snapshot: {len(snapshot)} skills",
                f"Roles: {', '.join(roles) if roles else 'unknown'}",
                f"Fingerprint: {agent.fingerprint[:16]}",
            ],
            "raw_diff": "",
            "fallback_agent_name": _fallback_existing_agent_name(agent, previous_agent),
            "fallback_changelog": agent.changelog or "Genesis: initial skill library snapshot",
            "fallback_lore": initial_fallback_lore(
                _fallback_existing_agent_name(agent, previous_agent),
                len(snapshot),
                roles,
            ),
        }

    rewrite_summary = _summarize_snapshot_rewrite(changed)
    if rewrite_summary:
        agent_name = _fallback_existing_agent_name(agent, previous_agent)
        return {
            "event_type": "identity_backfill_snapshot_rewrite",
            "previous_agent_name": previous_agent.agent_name if previous_agent else "无",
            "previous_born_at": previous_agent.born_at.isoformat() if previous_agent and previous_agent.born_at else "",
            "previous_lore": previous_agent.lore if previous_agent else "",
            "existing_agent_names": [
                name for name in _existing_agent_names(session)
                if name != agent.agent_name
            ],
            "trigger_skill_name": "全局策略快照重整",
            "trigger_skill_role": ", ".join(roles) if roles else "unknown",
            "previous_version": "旧策略键集合",
            "new_version": "新策略键集合",
            "born_at": agent.born_at.isoformat() if agent.born_at else _utc_now().isoformat(),
            "skill_versions_json": snapshot,
            "diff_summary": rewrite_summary,
            "raw_diff": "",
            "fallback_agent_name": agent_name,
            "fallback_changelog": fallback_changelog(
                previous_agent,
                "全局策略快照重整",
                "旧策略键集合",
                "新策略键集合",
                rewrite_summary,
            ),
            "fallback_lore": fallback_lore(
                agent_name,
                previous_agent,
                "全局策略快照重整",
                "旧策略键集合",
                "新策略键集合",
                rewrite_summary,
            ),
        }

    trigger_skill_name, previous_version, new_version = changed[0]
    facts = build_evolution_facts(
        session,
        previous_agent,
        trigger_skill_name,
        previous_version,
        new_version,
        snapshot,
        agent.born_at or _utc_now(),
    )
    facts["event_type"] = "identity_backfill"
    facts["existing_agent_names"] = [
        name for name in facts.get("existing_agent_names", [])
        if name != agent.agent_name
    ]
    facts["fallback_agent_name"] = _fallback_existing_agent_name(agent, previous_agent)
    facts["fallback_changelog"] = agent.changelog or facts["fallback_changelog"]
    facts["fallback_lore"] = fallback_lore(
        facts["fallback_agent_name"],
        previous_agent,
        trigger_skill_name,
        previous_version,
        new_version,
        facts.get("diff_summary") or [],
    )
    if len(changed) > 1:
        facts["diff_summary"] = list(facts.get("diff_summary") or []) + [
            f"Also changed: {skill_name} {old_version}->{new_version}"
            for skill_name, old_version, new_version in changed[1:4]
        ]
    return facts


def _changed_skill_versions(
    previous_snapshot: dict[str, str],
    snapshot: dict[str, str],
) -> list[tuple[str, str, str]]:
    changed: list[tuple[str, str, str]] = []
    for skill_name in sorted(set(previous_snapshot) | set(snapshot)):
        previous_version = previous_snapshot.get(skill_name, "none")
        new_version = snapshot.get(skill_name, "none")
        if previous_version != new_version:
            changed.append((skill_name, previous_version, new_version))
    return changed


def _summarize_snapshot_rewrite(changed: list[tuple[str, str, str]]) -> list[str] | None:
    removed = [name for name, old, new in changed if old != "none" and new == "none"]
    added = [name for name, old, new in changed if old == "none" and new != "none"]
    if len(removed) < 5 or len(added) < 5:
        return None
    return [
        f"全局策略快照重整：移除 {len(removed)} 个旧策略键，接入 {len(added)} 个新策略键",
        "策略命名和版本集合发生批量迁移，后续身份应以新快照为准",
    ]


def _fallback_existing_agent_name(
    agent: ConjugateAgent,
    previous_agent: ConjugateAgent | None,
) -> str:
    current_name = str(agent.agent_name or "").strip()
    if _agent_name_in_spec(current_name):
        return current_name
    return fallback_agent_name(previous_agent)


def build_evolution_facts(
    session,
    previous_agent: ConjugateAgent | None,
    trigger_skill_name: str,
    previous_version: str,
    new_version: str,
    snapshot: dict[str, str],
    born_at: datetime,
) -> dict[str, Any]:
    skill = session.query(EvolutionSkill).filter_by(skill_name=trigger_skill_name).first()
    old_content = _load_version_content(session, skill, previous_version) if skill else ""
    new_content = _load_version_content(session, skill, new_version) if skill else ""
    diff_summary = _summarize_markdown_diff(old_content, new_content)
    raw_diff = "\n".join(
        difflib.unified_diff(
            _meaningful_lines(old_content),
            _meaningful_lines(new_content),
            fromfile=previous_version,
            tofile=new_version,
            lineterm="",
        )
    )
    fallback_name = fallback_agent_name(previous_agent)
    changelog = fallback_changelog(
        previous_agent,
        trigger_skill_name,
        previous_version,
        new_version,
        diff_summary,
    )
    return {
        "previous_agent_name": previous_agent.agent_name if previous_agent else "无",
        "previous_born_at": previous_agent.born_at.isoformat() if previous_agent and previous_agent.born_at else "",
        "previous_lore": previous_agent.lore if previous_agent else "",
        "existing_agent_names": _existing_agent_names(session),
        "trigger_skill_name": trigger_skill_name,
        "trigger_skill_role": skill.role if skill else "unknown",
        "previous_version": previous_version,
        "new_version": new_version,
        "born_at": born_at.isoformat() if born_at else _utc_now().isoformat(),
        "skill_versions_json": snapshot,
        "diff_summary": diff_summary,
        "raw_diff": _truncate(raw_diff, 4000),
        "fallback_agent_name": fallback_name,
        "fallback_changelog": changelog,
        "fallback_lore": fallback_lore(
            fallback_name,
            previous_agent,
            trigger_skill_name,
            previous_version,
            new_version,
            diff_summary,
        ),
    }


def generate_agent_identity(facts: dict[str, Any]) -> dict[str, str]:
    delay = _identity_retry_initial_seconds()
    max_delay = _identity_retry_max_seconds()
    attempt = 1
    while True:
        try:
            identity = _generate_agent_identity_with_llm(facts)
            return _normalize_identity(identity, facts, allow_fallback=False)
        except Exception as exc:
            logger.warning(
                "Failed to generate valid conjugate agent identity with LLM; "
                "retrying in %.1fs (attempt=%s, error=%s)",
                delay,
                attempt,
                exc,
                exc_info=True,
            )
            time.sleep(delay)
            delay = min(delay * 2, max_delay)
            attempt += 1


def _generate_agent_identity_with_llm(facts: dict[str, Any]) -> dict[str, Any]:
    from agents.llm_caller import llm
    from evolution.config import load_config

    prompt = build_agent_identity_prompt(facts)
    model = llm.model or load_config().clustering_model
    llm.model = model
    resp = llm.client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "你只输出合法 JSON，不要使用 Markdown，不要解释过程。",
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.8,
    )
    content = (resp.choices[0].message.content or "").strip()
    return _parse_json_object(content)


def build_agent_identity_prompt(facts: dict[str, Any]) -> str:
    return (
        "你是一个狼人杀 AI Agent 进化系统的角色命名师、技术叙事设计师和 3A 游戏角色设定编剧。\n\n"
        "你将收到一次 Agent 进化事件的结构化事实。请基于事实生成一个新的共轭 Agent 身份。\n\n"
        "重要概念：\n"
        "一个 Agent = 全局所有 skill 的 current_default 快照。\n"
        "本次进化表示某个 skill 的默认版本晋升，导致全局共轭指纹变化，因此诞生一个新 Agent。\n"
        "新 Agent 不是完全重生，而是前代 Agent 的进化形态。\n\n"
        "请严格遵守：\n"
        "1. 不要编造未提供的 skill 变化。\n"
        "2. agent_name 必须独特、有辨识度。\n"
        "3. agent_name 不要使用通用占位词，不要包含 Agent、版本号、ID、哈希。\n"
        "4. changelog 使用自然语言描述技术变化，但必须可追溯到输入的 diff 和版本变化。\n"
        "5. lore 偏艺术化、宣发化，可以用隐喻表达技术变化，但不能与 changelog 的事实冲突。\n"
        "6. 输出必须是合法 JSON，不要输出 Markdown，不要解释过程。\n\n"
        "输入事实：\n\n"
        "前代 Agent：\n"
        f"- name: {facts.get('previous_agent_name')}\n"
        f"- born_at: {facts.get('previous_born_at')}\n"
        f"- lore:\n{facts.get('previous_lore') or '无'}\n\n"
        "本次进化触发：\n"
        f"- skill_name: {facts.get('trigger_skill_name')}\n"
        f"- role: {facts.get('trigger_skill_role')}\n"
        f"- from_version: {facts.get('previous_version')}\n"
        f"- to_version: {facts.get('new_version')}\n"
        f"- born_at: {facts.get('born_at')}\n\n"
        "全局 skill 快照：\n"
        f"{json.dumps(facts.get('skill_versions_json') or {}, ensure_ascii=False, sort_keys=True)}\n\n"
        "结构化版本差异：\n"
        f"{json.dumps(facts.get('diff_summary') or [], ensure_ascii=False)}\n\n"
        "原始 diff 摘要：\n"
        f"{facts.get('raw_diff') or '无'}\n\n"
        "已有 Agent 名称，不能重复：\n"
        f"{json.dumps(facts.get('existing_agent_names') or [], ensure_ascii=False)}\n\n"
        "请生成 JSON，格式如下：\n\n"
        "{\n"
        '  "agent_name": "随机2或3个中文字符。不延续先前命名风格。根据主要进化的角色来更换取名方向。",\n'
        '  "changelog": "120到220字。技术变更描述，说明本次 skill 从旧版本到新版本的核心策略变化、行为倾向变化、可能影响的对局阶段。不要泛泛而谈。",\n'
        '  "lore": "200到300字。3A 游戏角色介绍风格，讲述名字来源、人格特质、命运轨迹，并用隐喻呼应本次技术进化。可体现与前代 Agent 的血脉连续性。"\n'
        "}\n"
    )


def _parse_json_object(content: str) -> dict[str, Any]:
    text = _strip_json_fence(content.strip())
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        json_text = text[start:end + 1]
        try:
            parsed = json.loads(json_text)
        except json.JSONDecodeError:
            from json_repair import loads as repair_json_loads

            parsed = repair_json_loads(json_text)
    if not isinstance(parsed, dict):
        raise ValueError("LLM identity response is not a JSON object")
    return parsed


def _strip_json_fence(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return text


def _normalize_identity(
    identity: dict[str, Any],
    facts: dict[str, Any],
    *,
    allow_fallback: bool = True,
) -> dict[str, str]:
    fallback = _fallback_identity(facts)
    existing_names = set(facts.get("existing_agent_names") or [])
    agent_name = str(identity.get("agent_name") or "").strip()
    changelog = str(identity.get("changelog") or "").strip()
    lore = str(identity.get("lore") or "").strip()

    if not agent_name or agent_name in existing_names or not _agent_name_in_spec(agent_name):
        if not allow_fallback:
            raise ValueError(f"LLM identity agent_name is invalid or duplicated: {agent_name!r}")
        agent_name = fallback["agent_name"]
    if not changelog:
        if not allow_fallback:
            raise ValueError("LLM identity changelog is empty")
        changelog = fallback["changelog"]
    if not lore:
        if not allow_fallback:
            raise ValueError("LLM identity lore is empty")
        lore = fallback["lore"]

    return {
        "agent_name": agent_name,
        "changelog": changelog,
        "lore": lore,
    }


def _fallback_identity(facts: dict[str, Any]) -> dict[str, str]:
    return {
        "agent_name": str(facts.get("fallback_agent_name") or "影刃"),
        "changelog": str(facts.get("fallback_changelog") or "策略默认版本发生晋升。"),
        "lore": str(facts.get("fallback_lore") or ""),
    }


def _agent_name_in_spec(name: str) -> bool:
    return bool(_AGENT_NAME_RE.fullmatch(str(name or "").strip()))


def _identity_retry_initial_seconds() -> float:
    return _positive_float_env("CONJUGATE_IDENTITY_RETRY_INITIAL_SECONDS", _IDENTITY_RETRY_INITIAL_SECONDS)


def _identity_retry_max_seconds() -> float:
    return _positive_float_env("CONJUGATE_IDENTITY_RETRY_MAX_SECONDS", _IDENTITY_RETRY_MAX_SECONDS)


def _positive_float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def fallback_changelog(
    previous_agent: ConjugateAgent | None,
    trigger_skill_name: str,
    previous_version: str,
    new_version: str,
    diff_summary: list[str],
) -> str:
    from_text = _format_version_label(previous_version)
    to_text = _format_version_label(new_version)
    lines = [
        f"共轭体默认策略变更：{trigger_skill_name} 从 {from_text} 调整到 {to_text}。",
    ]
    if diff_summary:
        lines.append("核心变化包括：" + "；".join(_clean_diff_focus(item) for item in diff_summary[:3]) + "。")
    else:
        lines.append("策略文本发生更新，但没有检测到可独立摘录的段落级变化。")
    return "\n".join(lines)


def initial_fallback_lore(agent_name: str, snapshot_count: int, roles: list[str]) -> str:
    return (
        f"{agent_name} 是共轭谱系的初代形态，携带 {snapshot_count} 个策略快照"
        f"与 {', '.join(roles) if roles else 'unknown'} 角色覆盖。"
        "它还没有前代记忆，却已经把夜战、推理与伪装的基础本能封存在同一枚指纹中。"
    )


def fallback_lore(
    agent_name: str,
    previous_agent: ConjugateAgent | None,
    trigger_skill_name: str,
    previous_version: str,
    new_version: str,
    diff_summary: list[str],
) -> str:
    previous_name = previous_agent.agent_name if previous_agent and previous_agent.agent_name else "前代共轭体"
    focus = _clean_diff_focus(diff_summary[0]) if diff_summary else "策略文本的关键变化"
    from_text = _format_version_label(previous_version)
    to_text = _format_version_label(new_version)
    return (
        f"{agent_name} 承接 {previous_name} 的判断习惯，在 {trigger_skill_name} "
        f"由 {from_text} 调整为 {to_text} 后成形。"
        f"它更关注 {focus}，因此在发言、站边和身份管理上会保留这次变化的痕迹。"
        "这不是换一张脸，而是一次有来源的性格偏移。"
    )


def _clean_diff_focus(text: str) -> str:
    focus = str(text or "").strip()
    for prefix in ("Added: ", "Removed: ", "Modified: "):
        if focus.startswith(prefix):
            focus = focus[len(prefix):]
            break
    focus = focus.strip().strip('"')
    for prefix in ("description: ", "role: ", "version: ", "tags: "):
        if focus.startswith(prefix):
            focus = focus[len(prefix):]
            break
    return _truncate(focus, 96) if focus else "策略文本的关键变化"


def _format_version_label(version: str) -> str:
    if version == "none":
        return "未启用"
    return version


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


def _existing_agent_names(session) -> list[str]:
    rows = session.query(ConjugateAgent.agent_name).order_by(ConjugateAgent.id.asc()).all()
    return [str(row[0]) for row in rows if row[0]]
