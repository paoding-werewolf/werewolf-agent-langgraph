#!/usr/bin/env python3
"""Repair restored evolution skill metadata and escaped Markdown content.

Default mode is dry-run. Apply mode requires both:

  WEREWOLF_METADATA_REPAIR=REPAIR_EVOLUTION_METADATA \
    python3 scripts/repair_evolution_skill_metadata.py --apply
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
sys.path.insert(0, str(ROOT / "app"))

from evolution.db import DATABASE_URL  # noqa: E402


CONFIRM_ENV = "WEREWOLF_METADATA_REPAIR"
CONFIRM_VALUE = "REPAIR_EVOLUTION_METADATA"


def normalize_content(content: str) -> str:
    """Convert fully escaped Markdown newlines back to real newlines."""
    if not content:
        return content
    if "\\n" in content and "\n" not in content:
        return content.replace("\\r\\n", "\n").replace("\\n", "\n")
    return content


def strip_markdown_fence(content: str) -> str:
    value = normalize_content(content or "").strip()
    if not value.startswith("```"):
        return value

    first_newline = value.find("\n")
    if first_newline < 0:
        return value
    value = value[first_newline + 1 :]
    if value.rstrip().endswith("```"):
        value = value.rstrip()[:-3]
    return value.strip()


def extract_frontmatter(content: str) -> dict[str, Any]:
    value = strip_markdown_fence(content)
    if not value.startswith("---"):
        return {}

    end = value.find("\n---", 3)
    if end < 0:
        return {}

    try:
        meta = yaml.safe_load(value[3:end]) or {}
    except Exception:
        return {}
    return meta if isinstance(meta, dict) else {}


def normalize_tags(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    tags = []
    for item in value:
        tag = str(item).strip()
        if tag:
            tags.append(tag)
    return tags


def is_empty_tags(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, list):
        return len(value) == 0
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, list) and len(parsed) == 0


def load_plan(conn) -> dict[str, list[dict[str, Any]]]:
    version_updates = []
    for row in conn.execute(text(
        """
        SELECT id, skill_id, version, content_markdown
        FROM evolution_skill_versions
        ORDER BY id
        """
    )).mappings():
        original = row["content_markdown"] or ""
        repaired = normalize_content(original)
        if repaired != original:
            version_updates.append({
                "id": row["id"],
                "skill_id": row["skill_id"],
                "version": row["version"],
                "content_markdown": repaired,
                "literal_newline_count": original.count("\\n"),
            })

    skill_updates = []
    for row in conn.execute(text(
        """
        SELECT s.id, s.skill_name, s.description, s.tags_json,
               v.content_markdown
        FROM evolution_skills s
        LEFT JOIN evolution_skill_versions v
          ON v.skill_id = s.id AND v.version = s.current_default
        ORDER BY s.skill_name
        """
    )).mappings():
        content = normalize_content(row["content_markdown"] or "")
        meta = extract_frontmatter(content)
        desc = str(meta.get("description") or "").strip()
        tags = normalize_tags(meta.get("tags"))

        needs_desc = not str(row["description"] or "").strip() and bool(desc)
        needs_tags = is_empty_tags(row["tags_json"]) and bool(tags)
        if needs_desc or needs_tags:
            skill_updates.append({
                "id": row["id"],
                "skill_name": row["skill_name"],
                "description": desc if needs_desc else row["description"],
                "tags_json": tags if needs_tags else row["tags_json"],
                "updated_fields": [
                    name
                    for name, needed in (("description", needs_desc), ("tags_json", needs_tags))
                    if needed
                ],
            })

    return {
        "version_updates": version_updates,
        "skill_updates": skill_updates,
    }


def apply_plan(conn, plan: dict[str, list[dict[str, Any]]]) -> None:
    for item in plan["version_updates"]:
        conn.execute(text(
            """
            UPDATE evolution_skill_versions
            SET content_markdown = :content_markdown,
                updated_at = NOW()
            WHERE id = :id
            """
        ), {
            "id": item["id"],
            "content_markdown": item["content_markdown"],
        })

    for item in plan["skill_updates"]:
        conn.execute(text(
            """
            UPDATE evolution_skills
            SET description = :description,
                tags_json = :tags_json,
                updated_at = NOW()
            WHERE id = :id
            """
        ), {
            "id": item["id"],
            "description": item["description"],
            "tags_json": json.dumps(item["tags_json"], ensure_ascii=False),
        })


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--sample", type=int, default=20)
    args = parser.parse_args()

    engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=3600)
    with engine.begin() as conn:
        plan = load_plan(conn)
        summary = {
            "database_url": DATABASE_URL.rsplit("@", 1)[-1],
            "mode": "apply" if args.apply else "dry-run",
            "version_content_updates": len(plan["version_updates"]),
            "skill_metadata_updates": len(plan["skill_updates"]),
            "version_samples": [
                {
                    "id": item["id"],
                    "skill_id": item["skill_id"],
                    "version": item["version"],
                    "literal_newline_count": item["literal_newline_count"],
                }
                for item in plan["version_updates"][:args.sample]
            ],
            "skill_samples": [
                {
                    "skill_name": item["skill_name"],
                    "updated_fields": item["updated_fields"],
                    "description_len": len(item["description"] or ""),
                    "tag_count": len(item["tags_json"] if isinstance(item["tags_json"], list) else []),
                }
                for item in plan["skill_updates"][:args.sample]
            ],
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))

        if args.apply:
            if os.getenv(CONFIRM_ENV) != CONFIRM_VALUE:
                raise SystemExit(
                    f"Refusing to apply without {CONFIRM_ENV}={CONFIRM_VALUE}"
                )
            apply_plan(conn, plan)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
