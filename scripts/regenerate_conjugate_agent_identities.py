#!/usr/bin/env python3
"""Regenerate conjugate agent identities while preserving database ids.

Default mode is dry-run and only writes a JSON preview file. Apply mode requires:

  WEREWOLF_CONJUGATE_REGEN=REGENERATE_CONJUGATE_IDENTITIES \
    python3 scripts/regenerate_conjugate_agent_identities.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from evolution.conjugate_agent import (  # noqa: E402
    agent_identity_needs_regeneration,
    build_existing_agent_identity_facts,
    fallback_agent_name,
    generate_agent_identity,
    generate_avatar_seed,
)
from evolution.db import DATABASE_URL, get_session  # noqa: E402
from evolution.models import ConjugateAgent  # noqa: E402


CONFIRM_ENV = "WEREWOLF_CONJUGATE_REGEN"
CONFIRM_VALUE = "REGENERATE_CONJUGATE_IDENTITIES"


@dataclass
class AgentSnapshot:
    id: int
    fingerprint: str
    agent_name: str
    avatar_seed: str
    born_at: Any
    skill_versions_json: dict[str, str]
    changelog: str
    lore: str


def to_snapshot(agent: ConjugateAgent, *, blank_identity: bool = False) -> AgentSnapshot:
    return AgentSnapshot(
        id=agent.id,
        fingerprint=agent.fingerprint,
        agent_name="" if blank_identity else (agent.agent_name or ""),
        avatar_seed=agent.avatar_seed or "",
        born_at=agent.born_at,
        skill_versions_json=dict(agent.skill_versions_json or {}),
        changelog="" if blank_identity else (agent.changelog or ""),
        lore="" if blank_identity else (agent.lore or ""),
    )


def parse_ids(raw: str) -> set[int] | None:
    if not raw.strip():
        return None
    return {int(part.strip()) for part in raw.split(",") if part.strip()}


def build_plan(
    target_ids: set[int] | None,
    use_llm: bool,
    fallback_only: bool,
) -> list[dict[str, Any]]:
    session = get_session()
    try:
        agents = (
            session.query(ConjugateAgent)
            .order_by(ConjugateAgent.id.asc())
            .all()
        )
        selected = [
            agent
            for agent in agents
            if (target_ids is None or agent.id in target_ids)
            and (not fallback_only or agent_identity_needs_regeneration(agent))
        ]
        missing_ids = sorted((target_ids or set()) - {agent.id for agent in selected})
        if missing_ids:
            raise SystemExit(f"Unknown conjugate agent ids: {missing_ids}")

        generated_names: set[str] = set()
        plan: list[dict[str, Any]] = []
        previous: AgentSnapshot | None = None
        selected_ids = {agent.id for agent in selected}
        for agent in agents:
            if agent.id not in selected_ids:
                previous = to_snapshot(agent)
                continue

            blank_agent = to_snapshot(agent, blank_identity=True)
            facts = build_existing_agent_identity_facts(session, blank_agent, previous)
            facts["existing_agent_names"] = sorted(
                (set(facts.get("existing_agent_names") or []) - {agent.agent_name})
                | generated_names
            )
            facts["fallback_agent_name"] = fallback_agent_name(previous)

            identity = (
                generate_agent_identity(facts)
                if use_llm
                else {
                    "agent_name": facts["fallback_agent_name"],
                    "changelog": facts.get("fallback_changelog") or "",
                    "lore": facts.get("fallback_lore") or "",
                }
            )
            generated_names.add(identity["agent_name"])

            item = {
                "id": agent.id,
                "fingerprint": agent.fingerprint,
                "snapshot_skill_count": len(agent.skill_versions_json or {}),
                "trigger_skill_name": facts.get("trigger_skill_name"),
                "previous_version": facts.get("previous_version"),
                "new_version": facts.get("new_version"),
                "old": {
                    "agent_name": agent.agent_name or "",
                    "avatar_seed": agent.avatar_seed or "",
                    "changelog": agent.changelog or "",
                    "lore": agent.lore or "",
                },
                "new": {
                    "agent_name": identity["agent_name"],
                    "avatar_seed": generate_avatar_seed(agent.fingerprint),
                    "changelog": identity["changelog"],
                    "lore": identity["lore"],
                },
            }
            plan.append(item)

            previous = AgentSnapshot(
                id=agent.id,
                fingerprint=agent.fingerprint,
                agent_name=identity["agent_name"],
                avatar_seed=generate_avatar_seed(agent.fingerprint),
                born_at=agent.born_at,
                skill_versions_json=dict(agent.skill_versions_json or {}),
                changelog=identity["changelog"],
                lore=identity["lore"],
            )

        return plan
    finally:
        session.close()


def apply_plan(plan: list[dict[str, Any]], refresh_avatar_seed: bool) -> None:
    session = get_session()
    try:
        for item in plan:
            agent = session.get(ConjugateAgent, item["id"])
            if not agent:
                raise RuntimeError(f"Conjugate agent disappeared: id={item['id']}")
            if agent.fingerprint != item["fingerprint"]:
                raise RuntimeError(
                    f"Fingerprint changed for id={item['id']}: "
                    f"{agent.fingerprint} != {item['fingerprint']}"
                )
            agent.agent_name = item["new"]["agent_name"]
            if refresh_avatar_seed:
                agent.avatar_seed = item["new"]["avatar_seed"]
            agent.changelog = item["new"]["changelog"]
            agent.lore = item["new"]["lore"]
            agent.updated_at = datetime.now()
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def write_preview(path: Path, plan: list[dict[str, Any]], mode: str, use_llm: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "database_url": DATABASE_URL.rsplit("@", 1)[-1],
        "mode": mode,
        "use_llm": use_llm,
        "count": len(plan),
        "agents": plan,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def load_preview(path: Path) -> tuple[list[dict[str, Any]], bool]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    agents = payload.get("agents")
    if not isinstance(agents, list):
        raise SystemExit(f"Invalid preview file: {path}")
    for item in agents:
        if not isinstance(item, dict) or "id" not in item or "fingerprint" not in item:
            raise SystemExit(f"Invalid preview agent item in {path}")
        if not isinstance(item.get("new"), dict):
            raise SystemExit(f"Missing new identity for id={item.get('id')}")
    return agents, bool(payload.get("use_llm"))


def print_summary(plan: list[dict[str, Any]], preview_path: Path, mode: str, sample: int) -> None:
    summary = {
        "mode": mode,
        "count": len(plan),
        "preview_path": str(preview_path),
        "samples": [
            {
                "id": item["id"],
                "snapshot_skill_count": item["snapshot_skill_count"],
                "trigger_skill_name": item["trigger_skill_name"],
                "version_change": f"{item['previous_version']}->{item['new_version']}",
                "old_name": item["old"]["agent_name"],
                "new_name": item["new"]["agent_name"],
                "new_changelog_head": item["new"]["changelog"][:160],
            }
            for item in plan[:sample]
        ],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--ids", default="", help="Comma-separated ids. Default: all agents.")
    parser.add_argument(
        "--fallback-only",
        action="store_true",
        help="Only regenerate agents with non-spec names or empty changelog/lore.",
    )
    parser.add_argument("--no-llm", action="store_true", help="Use deterministic fallbacks only.")
    parser.add_argument("--refresh-avatar-seed", action="store_true")
    parser.add_argument("--sample", type=int, default=10)
    parser.add_argument("--output", default="")
    parser.add_argument(
        "--input-preview",
        default="",
        help="Apply or summarize an existing preview JSON without regenerating identities.",
    )
    args = parser.parse_args()

    mode = "apply" if args.apply else "dry-run"
    if args.input_preview:
        preview_path = Path(args.input_preview).expanduser()
        if not preview_path.is_absolute():
            preview_path = ROOT / preview_path
        plan, use_llm = load_preview(preview_path)
    else:
        target_ids = parse_ids(args.ids)
        use_llm = not args.no_llm
        plan = build_plan(target_ids, use_llm, args.fallback_only)

        default_output = (
            ROOT / "backups" /
            f"conjugate_agent_identity_regen_preview_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        preview_path = Path(args.output).expanduser() if args.output else default_output
        if not preview_path.is_absolute():
            preview_path = ROOT / preview_path
        write_preview(preview_path, plan, mode, use_llm)
    print_summary(plan, preview_path, mode, args.sample)

    if args.apply:
        if os.getenv(CONFIRM_ENV) != CONFIRM_VALUE:
            raise SystemExit(
                f"Refusing to apply without {CONFIRM_ENV}={CONFIRM_VALUE}"
            )
        apply_plan(plan, args.refresh_avatar_seed)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
