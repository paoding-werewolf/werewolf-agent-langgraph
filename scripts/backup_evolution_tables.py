#!/usr/bin/env python3
"""Back up MySQL tables to a SQL file through SQLAlchemy/PyMySQL."""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict, deque
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from evolution.db import DATABASE_URL  # noqa: E402


DEFAULT_TABLES = [
    "evolution_skills",
    "evolution_skill_versions",
    "conjugate_agents",
    "evolution_buffer_items",
    "evolution_game_archive",
    "evolution_runtime_state",
]


def sql_string(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "0x" + bytes(value).hex()
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    if isinstance(value, (datetime, date)):
        value = value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text_value = str(value)
    return "'" + text_value.replace("\\", "\\\\").replace("'", "''") + "'"


def quote_ident(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def list_base_tables(conn) -> list[str]:
    rows = conn.execute(text(
        """
        SELECT table_name AS table_name
        FROM information_schema.tables
        WHERE table_schema = DATABASE()
          AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """
    )).mappings().all()
    return [row["table_name"] for row in rows]


def dependency_order(conn, tables: list[str]) -> list[str]:
    table_set = set(tables)
    deps: dict[str, set[str]] = {table: set() for table in tables}
    children: dict[str, set[str]] = defaultdict(set)
    rows = conn.execute(text(
        """
        SELECT table_name AS table_name, referenced_table_name AS referenced_table_name
        FROM information_schema.key_column_usage
        WHERE table_schema = DATABASE()
          AND referenced_table_name IS NOT NULL
        """
    )).mappings().all()
    for row in rows:
        child = row["table_name"]
        parent = row["referenced_table_name"]
        if child in table_set and parent in table_set and child != parent:
            deps[child].add(parent)
            children[parent].add(child)

    ready = deque(sorted(table for table, parents in deps.items() if not parents))
    ordered: list[str] = []
    while ready:
        table = ready.popleft()
        ordered.append(table)
        for child in sorted(children.get(table, [])):
            deps[child].discard(table)
            if not deps[child]:
                ready.append(child)

    if len(ordered) != len(tables):
        remaining = sorted(set(tables) - set(ordered))
        ordered.extend(remaining)
    return ordered


def write_table(conn, out, table: str) -> int:
    create_row = conn.execute(text(f"SHOW CREATE TABLE {quote_ident(table)}")).first()
    create_sql = create_row[1]
    out.write(f"\n-- ------------------------------------------------------------\n")
    out.write(f"-- Table structure for {table}\n")
    out.write(f"-- ------------------------------------------------------------\n")
    out.write(f"DROP TABLE IF EXISTS {quote_ident(table)};\n")
    out.write(f"{create_sql};\n\n")

    columns = conn.execute(text(f"SHOW COLUMNS FROM {quote_ident(table)}")).mappings().all()
    column_names = [row["Field"] for row in columns]
    quoted_columns = ", ".join(quote_ident(col) for col in column_names)

    row_count = 0
    result = conn.execution_options(stream_results=True).execute(
        text(f"SELECT * FROM {quote_ident(table)}")
    ).mappings()
    for row in result:
        values = ", ".join(sql_string(row[col]) for col in column_names)
        out.write(f"INSERT INTO {quote_ident(table)} ({quoted_columns}) VALUES ({values});\n")
        row_count += 1
    out.write(f"\n-- Dumped {row_count} rows from {table}\n")
    return row_count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", help="Output SQL backup path")
    parser.add_argument("--tables", nargs="*", default=DEFAULT_TABLES)
    parser.add_argument("--all-tables", action="store_true", help="Back up every base table in the current database")
    args = parser.parse_args()

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=3600)
    counts: dict[str, int] = {}
    with engine.connect() as conn, output.open("w", encoding="utf-8") as out:
        selected_tables = list_base_tables(conn) if args.all_tables else args.tables
        selected_tables = dependency_order(conn, selected_tables)

        conn.execute(text("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        conn.execute(text("START TRANSACTION WITH CONSISTENT SNAPSHOT"))

        out.write("-- werewolf logical backup\n")
        out.write(f"-- generated_at: {datetime.now().isoformat(timespec='seconds')}\n")
        out.write(f"-- source: {DATABASE_URL.rsplit('@', 1)[-1]}\n")
        out.write(f"-- tables: {', '.join(selected_tables)}\n")
        out.write("SET NAMES utf8mb4;\n")
        out.write("SET FOREIGN_KEY_CHECKS=0;\n")
        for table in selected_tables:
            counts[table] = write_table(conn, out, table)
        out.write("\nSET FOREIGN_KEY_CHECKS=1;\n")
        conn.execute(text("COMMIT"))

    output.chmod(0o600)
    print(json.dumps({"output": str(output), "counts": counts}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
