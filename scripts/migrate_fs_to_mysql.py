"""migrate_fs_to_mysql.py — 将共享卷文件系统中的旧自进化数据迁移到 MySQL

在服务器上运行:
  python3 migrate_fs_to_mysql.py /opt/werewolf-agent-data

迁移内容:
  - skills/.skill_index.json + */*/.versions.json + */*/v*.md → evolution_skills + evolution_skill_versions
  - policy_buffer/confirmed/*.yaml → evolution_buffer_items (confirmed)
  - policy_buffer/clusters/*.yaml → evolution_buffer_items (cluster)
  - memory/curator_state.json → evolution_runtime_state
  - memory/game_archive/games.db → evolution_game_archive
"""
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pymysql
import yaml

MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASS = os.getenv("MYSQL_PASS")
MYSQL_DB = os.getenv("MYSQL_DB", "werewolf")


def get_conn():
    if not MYSQL_PASS:
        raise RuntimeError("MYSQL_PASS is required")
    return pymysql.connect(host=MYSQL_HOST, port=MYSQL_PORT, user=MYSQL_USER,
                           password=MYSQL_PASS, database=MYSQL_DB, charset="utf8mb4")


def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).replace(tzinfo=None)
    except Exception:
        return None


def migrate_skills(base: Path, conn):
    """迁移 skills 目录"""
    index_path = base / "skills" / ".skill_index.json"
    if not index_path.exists():
        print("[SKIP] skills/.skill_index.json not found")
        return

    index = json.loads(index_path.read_text())
    cur = conn.cursor()

    for skill_info in index.get("skills", []):
        name = skill_info["name"]
        role = skill_info["role"]
        desc = skill_info.get("description", "")
        tags = skill_info.get("tags", [])
        current_default = skill_info.get("current_version", "0")
        if current_default == "0":
            current_default = "v0"

        # Read versions.json
        role_dir = None
        short_name = None
        for d in (base / "skills").iterdir():
            if d.is_dir():
                for sd in d.iterdir():
                    if sd.is_dir() and sd.name in name:
                        role_dir = d.name
                        short_name = sd.name
                        break

        if not short_name:
            # Try matching by role prefix
            parts = name.split("-", 1)
            if len(parts) == 2:
                role_dir = parts[0]
                short_name = parts[1]

        ver_path = base / "skills" / role_dir / short_name / ".versions.json"
        versions_data = {}
        if ver_path.exists():
            versions_data = json.loads(ver_path.read_text())
            current_default = versions_data.get("current_default", current_default)

        # Insert skill
        cur.execute(
            "SELECT id FROM evolution_skills WHERE skill_name = %s", (name,))
        if cur.fetchone():
            cur.execute(
                "UPDATE evolution_skills SET role=%s, description=%s, tags_json=%s, current_default=%s, updated_at=NOW() WHERE skill_name=%s",
                (role, desc, json.dumps(tags), current_default, name))
            print(f"  [UPDATE] skill: {name}")
        else:
            cur.execute(
                "INSERT INTO evolution_skills (skill_name, role, description, tags_json, current_default, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,NOW(),NOW())",
                (name, role, desc, json.dumps(tags), current_default))
            print(f"  [INSERT] skill: {name}")

        cur.execute("SELECT id FROM evolution_skills WHERE skill_name = %s", (name,))
        skill_id = cur.fetchone()[0]

        # Insert versions
        for ver_name, ver_meta in versions_data.get("versions", {}).items():
            md_path = base / "skills" / role_dir / short_name / f"{ver_name}.md"
            content_md = ""
            if md_path.exists():
                content_md = md_path.read_text()

            source = ver_meta.get("source", "")
            trigger_cluster = ver_meta.get("trigger_cluster")
            pinned = ver_meta.get("pinned", False)
            status = ver_meta.get("status", "candidate")
            usage = ver_meta.get("usage", {})
            games_played = usage.get("games_played", 0)
            wins = usage.get("wins", 0)
            win_rate = usage.get("win_rate", 0.0)
            last_used = parse_dt(usage.get("last_used"))
            created_at = parse_dt(ver_meta.get("created_at"))

            cur.execute(
                "SELECT id FROM evolution_skill_versions WHERE skill_id = %s AND version = %s",
                (skill_id, ver_name))
            if cur.fetchone():
                cur.execute(
                    "UPDATE evolution_skill_versions SET status=%s, source=%s, trigger_cluster_id=%s, pinned=%s, content_markdown=%s, games_played=%s, wins=%s, win_rate=%s, last_used_at=%s, updated_at=NOW() WHERE skill_id=%s AND version=%s",
                    (status, source, trigger_cluster, pinned, content_md, games_played, wins, win_rate, last_used, skill_id, ver_name))
                print(f"    [UPDATE] version: {ver_name}")
            else:
                cur.execute(
                    "INSERT INTO evolution_skill_versions (skill_id, version, status, source, trigger_cluster_id, pinned, content_markdown, games_played, wins, win_rate, last_used_at, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())",
                    (skill_id, ver_name, status, source, trigger_cluster, pinned, content_md, games_played, wins, win_rate, last_used, created_at))
                print(f"    [INSERT] version: {ver_name}")

    conn.commit()
    print("[DONE] skills migration")


def migrate_buffer(base: Path, conn):
    """迁移 policy_buffer 目录"""
    cur = conn.cursor()

    for item_type, dirname in [("confirmed", "confirmed"), ("cluster", "clusters")]:
        dir_path = base / "policy_buffer" / dirname
        if not dir_path.exists():
            print(f"[SKIP] policy_buffer/{dirname} not found")
            continue

        for f in sorted(dir_path.glob("*.yaml")):
            data = yaml.safe_load(f.read_text())
            cluster_id = data.get("cluster_id", f.stem)
            scene_tags = data.get("scene_tags", {})
            suggestions = data.get("suggestions", [])
            avg_causal = data.get("avg_causal_strength", 0.0)
            consistency = data.get("consistency_rate", 0.0)
            created_at = parse_dt(data.get("created_at"))

            target_skill = None
            cur.execute("SELECT skill_name FROM evolution_skills")
            for skill_row in cur.fetchall():
                sn = skill_row[0]
                if sn in cluster_id:
                    target_skill = sn
                    break

            # Build payload
            payload = {
                "cluster_id": cluster_id,
                "suggestions": suggestions,
            }
            preview_texts = [s.get("summary", "")[:200] for s in suggestions if s.get("summary")]

            cur.execute(
                "SELECT id FROM evolution_buffer_items WHERE item_key = %s AND item_type = %s",
                (cluster_id, item_type))
            existing = cur.fetchone()
            if existing:
                cur.execute(
                    "UPDATE evolution_buffer_items SET suggestion_count=%s, avg_causal_strength=%s, consistency_rate=%s, scene_tags_json=%s, preview_texts_json=%s, payload_json=%s, target_skill_name=%s, cluster_id=%s, updated_at=NOW() WHERE id=%s",
                    (len(suggestions), avg_causal, consistency, json.dumps(scene_tags), json.dumps(preview_texts), json.dumps(payload), target_skill, cluster_id, existing[0]))
                print(f"  [UPDATE] {item_type}: {cluster_id}")
            else:
                cur.execute(
                    "INSERT INTO evolution_buffer_items (item_type, item_key, cluster_id, target_skill_name, status, suggestion_count, avg_causal_strength, consistency_rate, scene_tags_json, preview_texts_json, payload_json, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())",
                    (item_type, cluster_id, cluster_id, target_skill, item_type, len(suggestions), avg_causal, consistency, json.dumps(scene_tags), json.dumps(preview_texts), json.dumps(payload), created_at))
                print(f"  [INSERT] {item_type}: {cluster_id}")

    conn.commit()
    print("[DONE] buffer migration")


def migrate_curator_state(base: Path, conn):
    """迁移 curator_state.json"""
    state_path = base / "memory" / "curator_state.json"
    if not state_path.exists():
        print("[SKIP] memory/curator_state.json not found")
        return

    state = json.loads(state_path.read_text())
    cur = conn.cursor()

    cur.execute("SELECT state_key FROM evolution_runtime_state WHERE state_key = 'curator'")
    if cur.fetchone():
        cur.execute(
            "UPDATE evolution_runtime_state SET payload_json=%s, updated_at=NOW() WHERE state_key='curator'",
            (json.dumps(state),))
    else:
        cur.execute(
            "INSERT INTO evolution_runtime_state (state_key, payload_json, created_at, updated_at) VALUES ('curator', %s, NOW(), NOW())",
            (json.dumps(state),))

    conn.commit()
    print("[DONE] curator_state migration")


def migrate_game_archive(base: Path, conn):
    """迁移 SQLite games.db → MySQL"""
    db_path = base / "memory" / "game_archive" / "games.db"
    if not db_path.exists():
        print("[SKIP] memory/game_archive/games.db not found")
        return

    sqlite_conn = sqlite3.connect(str(db_path))
    sqlite_cur = sqlite_conn.cursor()

    cur = conn.cursor()

    # Migrate games
    try:
        sqlite_cur.execute("SELECT game_id, my_role, result, day_count, scene_tags, reflection_report, full_trace, strategies_used, created_at FROM games")
        for row in sqlite_cur.fetchall():
            game_id, my_role, result, day_count, scene_tags_str, reflection_report, full_trace, strategies_used_str, created_at_str = row
            scene_tags = json.loads(scene_tags_str) if scene_tags_str else {}
            strategies_used = json.loads(strategies_used_str) if strategies_used_str else []
            payload = {
                "scene_tags": scene_tags,
                "reflection_report": reflection_report or "",
                "full_trace": full_trace or "",
                "strategies_used": strategies_used,
            }
            created_at = parse_dt(created_at_str)

            cur.execute("SELECT id FROM evolution_game_archive WHERE game_id = %s", (game_id,))
            if cur.fetchone():
                cur.execute(
                    "UPDATE evolution_game_archive SET my_role=%s, result=%s, day_count=%s, has_builtin_ai=%s, payload_json=%s, updated_at=NOW() WHERE game_id=%s",
                    (my_role, result, day_count, False, json.dumps(payload), game_id))
                print(f"  [UPDATE] game: {game_id}")
            else:
                cur.execute(
                    "INSERT INTO evolution_game_archive (game_id, room_id, my_role, result, day_count, has_builtin_ai, payload_json, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW())",
                    (game_id, game_id, my_role, result, day_count, False, json.dumps(payload), created_at))
                print(f"  [INSERT] game: {game_id}")
    except Exception as e:
        print(f"  [WARN] games migration error: {e}")

    # Migrate strategy_gaps
    try:
        sqlite_cur.execute("SELECT game_id, scene_description, gap_count FROM strategy_gaps")
        for row in sqlite_cur.fetchall():
            game_id, scene_desc, gap_count = row
            payload = {"game_id": game_id}

            cur.execute("SELECT id FROM evolution_strategy_gaps WHERE scene_description = %s", (scene_desc,))
            if cur.fetchone():
                cur.execute(
                    "UPDATE evolution_strategy_gaps SET gap_count=%s, payload_json=%s, updated_at=NOW() WHERE scene_description=%s",
                    (gap_count, json.dumps(payload), scene_desc))
            else:
                cur.execute(
                    "INSERT INTO evolution_strategy_gaps (scene_description, gap_count, payload_json, created_at, updated_at) VALUES (%s,%s,%s,NOW(),NOW())",
                    (scene_desc, gap_count, json.dumps(payload)))
                print(f"  [INSERT] gap: {scene_desc[:50]}")
    except Exception as e:
        print(f"  [WARN] strategy_gaps migration error: {e}")

    sqlite_conn.close()
    conn.commit()
    print("[DONE] game_archive migration")


def main():
    if len(sys.argv) < 2:
        print(f"Usage: python3 {sys.argv[0]} <agent-data-dir>")
        print(f"  e.g. python3 {sys.argv[0]} /opt/werewolf-agent-data")
        sys.exit(1)

    base = Path(sys.argv[1])
    if not base.exists():
        print(f"Error: {base} does not exist")
        sys.exit(1)

    print(f"=== Migrating from {base} to MySQL ===")
    print(f"    MySQL: {MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DB}")
    print()

    conn = get_conn()
    try:
        migrate_skills(base, conn)
        migrate_buffer(base, conn)
        migrate_curator_state(base, conn)
        migrate_game_archive(base, conn)
        print()
        print("=== Migration complete ===")

        # Verify counts
        cur = conn.cursor()
        for tbl in ["evolution_skills", "evolution_skill_versions", "evolution_buffer_items",
                     "evolution_strategy_gaps", "evolution_game_archive", "evolution_runtime_state"]:
            cur.execute(f"SELECT COUNT(*) FROM {tbl}")
            print(f"  {tbl}: {cur.fetchone()[0]} rows")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
