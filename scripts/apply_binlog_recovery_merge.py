#!/usr/bin/env python3
"""Merge binlog recovery SQL into live evolution skills by natural keys.

Default mode is dry-run. Apply mode is intentionally guarded:

  WEREWOLF_RECOVERY_APPLY=MERGE_BINLOG_RECOVERY \
    python scripts/apply_binlog_recovery_merge.py --apply

The merge is conservative:
- skips known test fixture skills unless --include-fixtures is passed
- maps old skill_id to current skill_name, then current live skill id
- inserts missing versions
- updates existing versions only when recovered content/statistics are better
- preserves live current_default when it is valid
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import create_engine, text


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(SCRIPT_DIR))

from evolution.db import DATABASE_URL  # noqa: E402
from plan_binlog_recovery_merge import (  # noqa: E402
    parse_recovery_file,
    latest_by_skill_name,
    latest_versions_by_skill_version,
    text_quality,
)


FIXTURE_SKILLS = {"wolf-logic", "seer-logic", "common-logic"}
CONFIRM_ENV = "WEREWOLF_RECOVERY_APPLY"
CONFIRM_VALUE = "MERGE_BINLOG_RECOVERY"


def parse_json_value(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return default


def role_key(role: str) -> str:
    return "wolf" if role in {"wolf", "wolf_king", "wolf-king", "狼人", "狼人（深水倒钩位）"} else role


def strip_markdown_fence(content: str) -> str:
    text_value = (content or "").strip()
    if text_value.startswith("```"):
        first_newline = text_value.find("\n")
        if first_newline >= 0:
            text_value = text_value[first_newline + 1 :]
        if text_value.endswith("```"):
            text_value = text_value[:-3]
    return text_value.strip()


def frontmatter(content: str) -> dict[str, Any]:
    text_value = strip_markdown_fence(content)
    if not text_value.startswith("---"):
        return {}
    end = text_value.find("\n---", 3)
    if end < 0:
        return {}
    try:
        raw = yaml.safe_load(text_value[3:end]) or {}
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def normalize_tags(value: Any) -> list[str]:
    tags = value if isinstance(value, list) else parse_json_value(value, [])
    if not isinstance(tags, list):
        return []
    return [str(tag) for tag in tags if str(tag).strip()]


def int_value(row: dict[str, Any], key: str) -> int:
    try:
        return int(row.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def float_value(row: dict[str, Any], key: str) -> float:
    try:
        return float(row.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def max_datetime(left: Any, right: Any) -> Any:
    if not left:
        return right
    if not right:
        return left
    return max(str(left), str(right))


def load_live(conn) -> tuple[dict[str, dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    skills = {
        row["skill_name"]: dict(row)
        for row in conn.execute(text(
            """
            SELECT id, skill_name, role, description, tags_json, current_default,
                   skill_games_played, skill_wins, skill_win_rate, created_at, updated_at
            FROM evolution_skills
            """
        )).mappings()
    }
    versions = {
        (row["skill_name"], row["version"]): dict(row)
        for row in conn.execute(text(
            """
            SELECT v.id, v.skill_id, s.skill_name, v.version, v.status, v.source,
                   v.trigger_cluster_id, v.pinned, v.content_markdown,
                   v.games_played, v.wins, v.win_rate, v.last_used_at,
                   v.created_at, v.updated_at
            FROM evolution_skill_versions v
            JOIN evolution_skills s ON s.id = v.skill_id
            """
        )).mappings()
    }
    return skills, versions


def metadata_for_skill(skill_name: str, skill_row: dict[str, Any], versions: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    preferred_version = str(skill_row.get("current_default") or "v1")
    content = ""
    preferred = versions.get((skill_name, preferred_version))
    if preferred:
        content = str(preferred.get("content_markdown") or "")
    if not content:
        candidates = [
            row for (name, _version), row in versions.items()
            if name == skill_name
        ]
        if candidates:
            content = str(max(candidates, key=lambda row: text_quality(row.get("content_markdown"))).get("content_markdown") or "")
    meta = frontmatter(content)
    desc = str(meta.get("description") or "").strip()
    tags = normalize_tags(meta.get("tags"))
    recovery_desc = str(skill_row.get("description") or "").strip()
    if not desc and text_quality(recovery_desc)[0] >= 2:
        desc = recovery_desc
    if not tags:
        tags = normalize_tags(skill_row.get("tags_json"))
    role = role_key(str(skill_row.get("role") or meta.get("role") or "common"))
    return {"description": desc, "tags": tags, "role": role}


def build_plan(recovery_dir: Path, include_fixtures: bool) -> dict[str, Any]:
    skill_rows = parse_recovery_file(recovery_dir / "recovery_evolution_skills.sql")
    version_rows = parse_recovery_file(recovery_dir / "recovery_evolution_skill_versions.sql")
    recovery_skills, old_id_to_name = latest_by_skill_name(skill_rows)
    recovery_versions = latest_versions_by_skill_version(version_rows, old_id_to_name)
    if not include_fixtures:
        recovery_skills = {
            name: row for name, row in recovery_skills.items()
            if name not in FIXTURE_SKILLS
        }
        recovery_versions = {
            key: row for key, row in recovery_versions.items()
            if key[0] not in FIXTURE_SKILLS
        }
    return {
        "skills": recovery_skills,
        "versions": recovery_versions,
        "excluded_fixture_skills": sorted(FIXTURE_SKILLS) if not include_fixtures else [],
    }


def merged_skill_values(
    skill_name: str,
    recovery_row: dict[str, Any],
    live_row: dict[str, Any] | None,
    recovery_versions: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    meta = metadata_for_skill(skill_name, recovery_row, recovery_versions)
    recovery_games = int_value(recovery_row, "skill_games_played")
    recovery_wins = int_value(recovery_row, "skill_wins")
    if live_row:
        live_games = int_value(live_row, "skill_games_played")
        live_wins = int_value(live_row, "skill_wins")
        games = max(live_games, recovery_games)
        wins = max(live_wins, recovery_wins)
        live_tags = normalize_tags(live_row.get("tags_json"))
        live_desc = str(live_row.get("description") or "")
        return {
            "role": live_row.get("role") or meta["role"],
            "description": live_desc or meta["description"],
            "tags_json": json.dumps(live_tags or meta["tags"], ensure_ascii=False),
            "current_default": live_row.get("current_default") or recovery_row.get("current_default") or "v1",
            "skill_games_played": games,
            "skill_wins": wins,
            "skill_win_rate": (wins / games) if games else max(float_value(live_row, "skill_win_rate"), float_value(recovery_row, "skill_win_rate")),
            "created_at": live_row.get("created_at") or recovery_row.get("created_at"),
            "updated_at": max_datetime(live_row.get("updated_at"), recovery_row.get("updated_at")),
        }
    games = recovery_games
    wins = recovery_wins
    return {
        "role": meta["role"],
        "description": meta["description"],
        "tags_json": json.dumps(meta["tags"], ensure_ascii=False),
        "current_default": recovery_row.get("current_default") or "v1",
        "skill_games_played": games,
        "skill_wins": wins,
        "skill_win_rate": (wins / games) if games else float_value(recovery_row, "skill_win_rate"),
        "created_at": recovery_row.get("created_at"),
        "updated_at": recovery_row.get("updated_at"),
    }


def merged_version_values(
    recovery_row: dict[str, Any],
    live_row: dict[str, Any] | None,
    skill_id: int,
) -> dict[str, Any]:
    if live_row:
        live_quality = text_quality(live_row.get("content_markdown"))
        recovery_quality = text_quality(recovery_row.get("content_markdown"))
        content = recovery_row.get("content_markdown") if recovery_quality > live_quality else live_row.get("content_markdown")
        games = max(int_value(live_row, "games_played"), int_value(recovery_row, "games_played"))
        wins = max(int_value(live_row, "wins"), int_value(recovery_row, "wins"))
        return {
            "id": live_row["id"],
            "skill_id": live_row["skill_id"],
            "version": live_row["version"],
            "status": live_row.get("status") or recovery_row.get("status") or "candidate",
            "source": live_row.get("source") or recovery_row.get("source") or "",
            "trigger_cluster_id": live_row.get("trigger_cluster_id") or recovery_row.get("trigger_cluster_id"),
            "pinned": bool(live_row.get("pinned")) or bool(recovery_row.get("pinned")),
            "content_markdown": content or "",
            "games_played": games,
            "wins": wins,
            "win_rate": (wins / games) if games else max(float_value(live_row, "win_rate"), float_value(recovery_row, "win_rate")),
            "last_used_at": max_datetime(live_row.get("last_used_at"), recovery_row.get("last_used_at")),
            "created_at": live_row.get("created_at") or recovery_row.get("created_at"),
            "updated_at": max_datetime(live_row.get("updated_at"), recovery_row.get("updated_at")),
        }
    games = int_value(recovery_row, "games_played")
    wins = int_value(recovery_row, "wins")
    return {
        "skill_id": skill_id,
        "version": recovery_row.get("version"),
        "status": recovery_row.get("status") or "candidate",
        "source": recovery_row.get("source") or "",
        "trigger_cluster_id": recovery_row.get("trigger_cluster_id"),
        "pinned": bool(recovery_row.get("pinned")),
        "content_markdown": recovery_row.get("content_markdown") or "",
        "games_played": games,
        "wins": wins,
        "win_rate": (wins / games) if games else float_value(recovery_row, "win_rate"),
        "last_used_at": recovery_row.get("last_used_at"),
        "created_at": recovery_row.get("created_at"),
        "updated_at": recovery_row.get("updated_at"),
    }


def apply_skill_insert(conn, skill_name: str, values: dict[str, Any]) -> None:
    conn.execute(text(
        """
        INSERT INTO evolution_skills
          (skill_name, role, description, tags_json, current_default,
           skill_games_played, skill_wins, skill_win_rate, created_at, updated_at)
        VALUES
          (:skill_name, :role, :description, :tags_json, :current_default,
           :skill_games_played, :skill_wins, :skill_win_rate,
           COALESCE(:created_at, NOW()), COALESCE(:updated_at, NOW()))
        """
    ), {"skill_name": skill_name, **values})


def apply_skill_update(conn, skill_name: str, values: dict[str, Any]) -> None:
    conn.execute(text(
        """
        UPDATE evolution_skills
        SET role=:role,
            description=:description,
            tags_json=:tags_json,
            current_default=:current_default,
            skill_games_played=:skill_games_played,
            skill_wins=:skill_wins,
            skill_win_rate=:skill_win_rate,
            updated_at=COALESCE(:updated_at, updated_at)
        WHERE skill_name=:skill_name
        """
    ), {"skill_name": skill_name, **values})


def apply_version_insert(conn, values: dict[str, Any]) -> None:
    conn.execute(text(
        """
        INSERT INTO evolution_skill_versions
          (skill_id, version, status, source, trigger_cluster_id, pinned,
           content_markdown, games_played, wins, win_rate, last_used_at,
           created_at, updated_at)
        VALUES
          (:skill_id, :version, :status, :source, :trigger_cluster_id, :pinned,
           :content_markdown, :games_played, :wins, :win_rate, :last_used_at,
           COALESCE(:created_at, NOW()), COALESCE(:updated_at, NOW()))
        """
    ), values)


def apply_version_update(conn, values: dict[str, Any]) -> None:
    conn.execute(text(
        """
        UPDATE evolution_skill_versions
        SET status=:status,
            source=:source,
            trigger_cluster_id=:trigger_cluster_id,
            pinned=:pinned,
            content_markdown=:content_markdown,
            games_played=:games_played,
            wins=:wins,
            win_rate=:win_rate,
            last_used_at=:last_used_at,
            updated_at=COALESCE(:updated_at, updated_at)
        WHERE id=:id
        """
    ), values)


def run(args: argparse.Namespace) -> dict[str, Any]:
    plan = build_plan(Path(args.recovery_dir), args.include_fixtures)
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=3600)
    report: dict[str, Any] = {
        "mode": "apply" if args.apply else "dry-run",
        "database_url": DATABASE_URL.rsplit("@", 1)[-1],
        "excluded_fixture_skills": plan["excluded_fixture_skills"],
        "skill_inserts": [],
        "skill_updates": [],
        "version_inserts": [],
        "version_updates": [],
    }

    context = engine.begin() if args.apply else engine.connect()
    with context as conn:
        live_skills, live_versions = load_live(conn)
        for skill_name, recovery_row in sorted(plan["skills"].items()):
            live_row = live_skills.get(skill_name)
            values = merged_skill_values(skill_name, recovery_row, live_row, plan["versions"])
            if live_row:
                report["skill_updates"].append(skill_name)
                if args.apply:
                    apply_skill_update(conn, skill_name, values)
            else:
                report["skill_inserts"].append(skill_name)
                if args.apply:
                    apply_skill_insert(conn, skill_name, values)

        if args.apply:
            live_skills, live_versions = load_live(conn)
        else:
            for skill_name in report["skill_inserts"]:
                live_skills[skill_name] = {"id": -1, "skill_name": skill_name}

        for key, recovery_row in sorted(plan["versions"].items()):
            skill_name, version = key
            skill = live_skills.get(skill_name)
            if not skill:
                report.setdefault("skipped_versions_missing_skill", []).append([skill_name, version])
                continue
            live_row = live_versions.get(key)
            values = merged_version_values(recovery_row, live_row, int(skill["id"]))
            if live_row:
                report["version_updates"].append([skill_name, version])
                if args.apply:
                    apply_version_update(conn, values)
            else:
                report["version_inserts"].append([skill_name, version])
                if args.apply:
                    apply_version_insert(conn, values)

    report["counts"] = {
        "skill_inserts": len(report["skill_inserts"]),
        "skill_updates": len(report["skill_updates"]),
        "version_inserts": len(report["version_inserts"]),
        "version_updates": len(report["version_updates"]),
        "skipped_versions_missing_skill": len(report.get("skipped_versions_missing_skill", [])),
    }
    sample_limit = args.sample
    report["samples"] = {
        "skill_inserts": report["skill_inserts"][:sample_limit],
        "skill_updates": report["skill_updates"][:sample_limit],
        "version_inserts": report["version_inserts"][:sample_limit],
        "version_updates": report["version_updates"][:sample_limit],
    }
    for key in ("skill_inserts", "skill_updates", "version_inserts", "version_updates"):
        report.pop(key, None)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recovery-dir", default=str(ROOT / "recovery"))
    parser.add_argument("--include-fixtures", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--sample", type=int, default=20)
    args = parser.parse_args()

    if args.apply and os.getenv(CONFIRM_ENV) != CONFIRM_VALUE:
        raise SystemExit(
            f"Refusing to apply. Set {CONFIRM_ENV}={CONFIRM_VALUE} after explicit approval."
        )

    report = run(args)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
