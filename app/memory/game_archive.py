"""memory/game_archive.py — 对局历史归档 (SQLite)

存储：~/.werewolf-agent/memory/game_archive/games.db
加载：按需检索（按角色/场景/对手过滤）
更新：每局结束后写入
"""
import sqlite3
import json
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime, timezone

from evolution.config import AGENT_HOME

DB_PATH = AGENT_HOME / "memory" / "game_archive" / "games.db"


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    _ensure_tables(conn)
    return conn


def _ensure_tables(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS games (
            game_id TEXT PRIMARY KEY,
            my_role TEXT,
            result TEXT,
            day_count INTEGER,
            scene_tags TEXT,
            reflection_report TEXT,
            full_trace TEXT,
            strategies_used TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS strategy_gaps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id TEXT,
            scene_description TEXT,
            gap_count INTEGER DEFAULT 1,
            FOREIGN KEY (game_id) REFERENCES games(game_id)
        );
        CREATE INDEX IF NOT EXISTS idx_games_role ON games(my_role);
        CREATE INDEX IF NOT EXISTS idx_games_result ON games(result);
    """)
    conn.commit()


def save_game(game_id: str, my_role: str, result: str, day_count: int,
              scene_tags: Dict, reflection_report: str, full_trace: str,
              strategies_used: List[str]):
    """保存一局对局记录。"""
    conn = get_connection()
    conn.execute("""
        INSERT OR REPLACE INTO games
        (game_id, my_role, result, day_count, scene_tags, reflection_report,
         full_trace, strategies_used, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        game_id, my_role, result, day_count,
        json.dumps(scene_tags, ensure_ascii=False),
        reflection_report, full_trace,
        json.dumps(strategies_used),
        datetime.now(timezone.utc).isoformat(),
    ))
    conn.commit()
    conn.close()


def query_games(my_role: Optional[str] = None, result: Optional[str] = None,
                limit: int = 10) -> List[Dict]:
    """按条件检索历史对局。"""
    conn = get_connection()
    query = "SELECT * FROM games WHERE 1=1"
    params = []
    if my_role:
        query += " AND my_role = ?"
        params.append(my_role)
    if result:
        query += " AND result = ?"
        params.append(result)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def record_strategy_gap(game_id: str, scene_description: str):
    """记录一次 strategy_gap（策略覆盖空白）。"""
    conn = get_connection()
    row = conn.execute(
        "SELECT id, gap_count FROM strategy_gaps WHERE scene_description = ?",
        (scene_description,)
    ).fetchone()

    if row:
        conn.execute(
            "UPDATE strategy_gaps SET gap_count = gap_count + 1 WHERE id = ?",
            (row["id"],)
        )
    else:
        conn.execute(
            "INSERT INTO strategy_gaps (game_id, scene_description) VALUES (?, ?)",
            (game_id, scene_description)
        )
    conn.commit()
    conn.close()


def get_frequent_gaps(min_count: int = 5) -> List[Dict]:
    """获取频繁出现的 strategy_gap（>= min_count 次）。"""
    conn = get_connection()
    rows = conn.execute(
        "SELECT scene_description, gap_count FROM strategy_gaps WHERE gap_count >= ? ORDER BY gap_count DESC",
        (min_count,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
