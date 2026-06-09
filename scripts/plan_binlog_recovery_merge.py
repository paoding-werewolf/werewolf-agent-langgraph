#!/usr/bin/env python3
"""Plan a safe merge from binlog recovery SQL into live evolution tables.

This script is read-only. It parses recovery/*.sql, connects through the same
DATABASE_URL used by the app, and prints what would be merged by natural keys:

- evolution_skills: skill_name
- evolution_skill_versions: skill_name + version
- conjugate_agents: fingerprint

It intentionally does not write to MySQL. Generate a plan first, review the
counts and samples, then create a separate audited apply script.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from evolution.db import DATABASE_URL  # noqa: E402


INSERT_RE = re.compile(r"INSERT INTO (\w+) \((.*?)\) VALUES \((.*)\);", re.S)


def split_values(values: str) -> list[str]:
    out: list[str] = []
    buf: list[str] = []
    in_string = False
    i = 0
    while i < len(values):
        ch = values[i]
        if in_string:
            buf.append(ch)
            if ch == "'":
                if i + 1 < len(values) and values[i + 1] == "'":
                    buf.append(values[i + 1])
                    i += 1
                else:
                    in_string = False
        else:
            if ch == "'":
                in_string = True
                buf.append(ch)
            elif ch == ",":
                out.append("".join(buf).strip())
                buf = []
            else:
                buf.append(ch)
        i += 1
    out.append("".join(buf).strip())
    return out


def unquote(value: str) -> Any:
    value = value.strip()
    if value.upper() == "NULL":
        return None
    if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        return value[1:-1].replace("''", "'")
    if value in {"0", "1"}:
        return int(value)
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value


def parse_recovery_file(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(errors="replace").splitlines():
        match = INSERT_RE.match(line)
        if not match:
            continue
        columns = [col.strip() for col in match.group(2).split(",")]
        values = [unquote(v) for v in split_values(match.group(3))]
        rows.append(dict(zip(columns, values)))
    return rows


MOJIBAKE_MARKERS = ("å", "ã", "ç‹", "é¢", "è¨", "¾…")


def text_quality(value: Any) -> tuple[int, int]:
    text_value = str(value or "").strip()
    if not text_value:
        return (1, 0)
    if any(marker in text_value for marker in MOJIBAKE_MARKERS):
        return (0, len(text_value))
    return (2, len(text_value))


def numeric_value(row: dict[str, Any], key: str) -> float:
    value = row.get(key) or 0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def skill_row_score(row: dict[str, Any]) -> tuple[float, int, int, str]:
    return (
        numeric_value(row, "skill_games_played"),
        text_quality(row.get("description"))[0],
        text_quality(row.get("description"))[1],
        str(row.get("updated_at") or ""),
    )


def version_row_score(row: dict[str, Any]) -> tuple[int, int, float, str]:
    quality, length = text_quality(row.get("content_markdown"))
    return (
        quality,
        length,
        numeric_value(row, "games_played"),
        str(row.get("updated_at") or ""),
    )


def latest_by_skill_name(rows: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    by_name: dict[str, dict[str, Any]] = {}
    old_id_to_name: dict[str, str] = {}
    for row in rows:
        name = str(row["skill_name"])
        old_id_to_name[str(row["id"])] = name
        current = by_name.get(name)
        if current is None or skill_row_score(row) > skill_row_score(current):
            by_name[name] = row
    return by_name, old_id_to_name


def latest_versions_by_skill_version(
    rows: list[dict[str, Any]],
    old_id_to_name: dict[str, str],
) -> dict[tuple[str, str], dict[str, Any]]:
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        skill_name = old_id_to_name.get(str(row["skill_id"]))
        if not skill_name:
            continue
        key = (skill_name, str(row["version"]))
        current = by_key.get(key)
        if current is None or version_row_score(row) > version_row_score(current):
            merged = dict(row)
            merged["skill_name"] = skill_name
            by_key[key] = merged
    return by_key


def load_live_state() -> dict[str, Any]:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=3600)
    with engine.connect() as conn:
        skills = conn.execute(text(
            """
            SELECT id, skill_name, role, description, tags_json, current_default,
                   skill_games_played, skill_wins, skill_win_rate, created_at, updated_at
            FROM evolution_skills
            """
        )).mappings().all()
        versions = conn.execute(text(
            """
            SELECT v.id, v.skill_id, s.skill_name, v.version, v.status,
                   v.games_played, v.wins, v.win_rate, v.created_at, v.updated_at
            FROM evolution_skill_versions v
            JOIN evolution_skills s ON s.id = v.skill_id
            """
        )).mappings().all()
        agents = conn.execute(text(
            "SELECT id, fingerprint, skill_versions_json, created_at, updated_at FROM conjugate_agents"
        )).mappings().all()
    return {
        "skills": {row["skill_name"]: dict(row) for row in skills},
        "versions": {(row["skill_name"], row["version"]): dict(row) for row in versions},
        "agents": {row["fingerprint"]: dict(row) for row in agents},
    }


def sample(items: list[Any], limit: int) -> list[Any]:
    return items[:limit]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--recovery-dir",
        default=str(ROOT / "recovery"),
        help="Directory containing recovery_*.sql files",
    )
    parser.add_argument("--sample", type=int, default=20)
    args = parser.parse_args()

    recovery_dir = Path(args.recovery_dir)
    skill_rows = parse_recovery_file(recovery_dir / "recovery_evolution_skills.sql")
    version_rows = parse_recovery_file(recovery_dir / "recovery_evolution_skill_versions.sql")
    agent_rows = parse_recovery_file(recovery_dir / "recovery_conjugate_agents.sql")

    recovery_skills, old_id_to_name = latest_by_skill_name(skill_rows)
    recovery_versions = latest_versions_by_skill_version(version_rows, old_id_to_name)
    recovery_agents = {str(row["fingerprint"]): row for row in agent_rows}
    live = load_live_state()

    skill_inserts = sorted(set(recovery_skills) - set(live["skills"]))
    skill_updates = sorted(set(recovery_skills) & set(live["skills"]))
    version_inserts = sorted(set(recovery_versions) - set(live["versions"]))
    version_updates = sorted(set(recovery_versions) & set(live["versions"]))
    agent_inserts = sorted(set(recovery_agents) - set(live["agents"]))
    agent_updates = sorted(set(recovery_agents) & set(live["agents"]))

    recovery_name_counts: dict[str, int] = {}
    for row in skill_rows:
        name = str(row.get("skill_name") or "")
        recovery_name_counts[name] = recovery_name_counts.get(name, 0) + 1
    duplicate_recovery_names = {
        name: count for name, count in recovery_name_counts.items() if count > 1
    }

    report = {
        "database_url": DATABASE_URL.rsplit("@", 1)[-1],
        "raw_recovery_counts": {
            "skills": len(skill_rows),
            "skill_versions": len(version_rows),
            "conjugate_agents": len(agent_rows),
        },
        "deduped_recovery_counts": {
            "skills_by_skill_name": len(recovery_skills),
            "versions_by_skill_name_version": len(recovery_versions),
            "agents_by_fingerprint": len(recovery_agents),
        },
        "live_counts": {
            "skills": len(live["skills"]),
            "skill_versions": len(live["versions"]),
            "conjugate_agents": len(live["agents"]),
        },
        "planned_merge_counts": {
            "skill_inserts": len(skill_inserts),
            "skill_updates": len(skill_updates),
            "version_inserts": len(version_inserts),
            "version_updates": len(version_updates),
            "agent_inserts": len(agent_inserts),
            "agent_updates": len(agent_updates),
        },
        "samples": {
            "duplicate_recovery_skill_names": sample(sorted(duplicate_recovery_names.items()), args.sample),
            "skill_inserts": sample(skill_inserts, args.sample),
            "skill_updates": sample(skill_updates, args.sample),
            "version_inserts": sample([list(key) for key in version_inserts], args.sample),
            "version_updates": sample([list(key) for key in version_updates], args.sample),
            "agent_insert_ids": sample([recovery_agents[fp].get("id") for fp in agent_inserts], args.sample),
        },
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
