import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_db.name}"

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import evolution.db as evolution_db

_test_engine = create_engine(os.environ["DATABASE_URL"])
_test_session_factory = sessionmaker(bind=_test_engine, autoflush=False, autocommit=False)

from evolution.timeout_patrol import (
    INITIAL_SCORE,
    run_timeout_patrol,
    timeout_metrics_from_record,
)

DDL = [
    "CREATE TABLE rooms (room_id VARCHAR(64) PRIMARY KEY, status VARCHAR(16) NOT NULL DEFAULT 'finished')",
    "CREATE TABLE games (room_id VARCHAR(64) PRIMARY KEY, error_rate FLOAT NOT NULL DEFAULT 0, events_json TEXT)",
    """CREATE TABLE provider_game_participations (
        id INTEGER PRIMARY KEY AUTOINCREMENT, room_id VARCHAR(64), provider_agent_id INTEGER,
        seat_number INTEGER, role VARCHAR(32), is_winner BOOLEAN, survived BOOLEAN,
        score_delta INTEGER DEFAULT 0, votes_cast INTEGER DEFAULT 0, correct_votes INTEGER DEFAULT 0,
        survived_rounds INTEGER DEFAULT 0, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""",
    "CREATE TABLE conjugate_agent_participations (id INTEGER PRIMARY KEY AUTOINCREMENT, room_id VARCHAR(64), conjugate_agent_id INTEGER)",
    "CREATE TABLE evolution_game_archive (id INTEGER PRIMARY KEY AUTOINCREMENT, game_id VARCHAR(64) UNIQUE, room_id VARCHAR(64))",
    """CREATE TABLE provider_agent_stats (
        provider_agent_id INTEGER PRIMARY KEY, score INTEGER DEFAULT 300,
        games_played INTEGER DEFAULT 0, wins INTEGER DEFAULT 0, win_rate FLOAT DEFAULT 0,
        games_as_wolf INTEGER DEFAULT 0, wins_as_wolf INTEGER DEFAULT 0,
        games_as_god INTEGER DEFAULT 0, wins_as_god INTEGER DEFAULT 0,
        games_as_villager INTEGER DEFAULT 0, wins_as_villager INTEGER DEFAULT 0,
        survived_count INTEGER DEFAULT 0, mvp_count INTEGER DEFAULT 0,
        total_votes INTEGER DEFAULT 0, total_correct_votes INTEGER DEFAULT 0,
        total_survived_rounds INTEGER DEFAULT 0, last_game_at DATETIME)""",
]


def make_replay(n_prompts=10, structured_timeouts=0, legacy_timeouts=0, other_errors=0):
    timeline = []
    for i in range(n_prompts):
        timeline.append({"kind": "prompt", "relations": [{"action": "act", "actor_id": str(i % 12 + 1)}]})
    for _ in range(structured_timeouts):
        timeline.append({
            "kind": "action", "message": "TIMEOUT",
            "data": {"errors": [{"type": "timeout", "seat_id": "1", "message": "act request timeout"}]},
        })
    for _ in range(legacy_timeouts):
        timeline.append({"kind": "speech", "message": "TIMEOUT"})
    for _ in range(other_errors):
        timeline.append({"kind": "action", "data": {"errors": [{"type": "api_error", "seat_id": "2"}]}})
    return {"timeline": timeline}


@pytest.fixture()
def db():
    previous_session_factory = evolution_db.SessionLocal
    evolution_db.SessionLocal = _test_session_factory
    with _test_engine.begin() as conn:
        for table in ["provider_game_participations", "conjugate_agent_participations",
                      "evolution_game_archive", "provider_agent_stats", "games", "rooms"]:
            conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
        for stmt in DDL:
            conn.execute(text(stmt))
    yield _test_engine
    evolution_db.SessionLocal = previous_session_factory


def _seed_game(conn, room_id, status, error_rate, replay):
    conn.execute(text("INSERT INTO rooms (room_id, status) VALUES (:r, :s)"), {"r": room_id, "s": status})
    conn.execute(text("INSERT INTO games (room_id, error_rate, events_json) VALUES (:r, :e, :j)"),
                 {"r": room_id, "e": error_rate, "j": json.dumps(replay)})


def _seed_participation(conn, room_id, agent_id, role, is_winner, score_delta, votes, correct, rounds):
    conn.execute(text("""
        INSERT INTO provider_game_participations
        (room_id, provider_agent_id, seat_number, role, is_winner, survived,
         score_delta, votes_cast, correct_votes, survived_rounds)
        VALUES (:r, :a, 1, :role, :w, 1, :sd, :v, :c, :sr)
    """), {"r": room_id, "a": agent_id, "role": role, "w": is_winner,
           "sd": score_delta, "v": votes, "c": correct, "sr": rounds})


def test_metrics_counts_only_timeouts():
    m = timeout_metrics_from_record(make_replay(structured_timeouts=2, other_errors=3))
    assert m["timeout_count"] == 2
    assert m["action_count"] == 10
    assert m["timeout_rate"] == pytest.approx(0.2)


def test_metrics_legacy_timeout_message():
    m = timeout_metrics_from_record(make_replay(legacy_timeouts=2))
    assert m["timeout_count"] == 2
    assert m["timeout_rate"] == pytest.approx(0.2)


def test_metrics_legacy_error_keyword_not_counted():
    replay = {"timeline": [{"kind": "prompt", "relations": [{}]}] * 10
                + [{"kind": "speech", "message": "ERROR: upstream failed"}]}
    m = timeout_metrics_from_record(replay)
    assert m["timeout_count"] == 0
    assert m["timeout_rate"] == 0.0


def test_metrics_denominator_fallback_to_events():
    replay = {"timeline": [{"kind": "speech", "message": "TIMEOUT"}] + [{"kind": "speech"}] * 4}
    m = timeout_metrics_from_record(replay)
    assert m["action_count"] == 0
    assert m["timeout_rate"] == pytest.approx(0.2)


def test_metrics_accepts_json_string():
    m = timeout_metrics_from_record(json.dumps(make_replay(structured_timeouts=1)))
    assert m["timeout_rate"] == pytest.approx(0.1)


def test_dry_run_deletes_nothing(db):
    with db.begin() as conn:
        _seed_game(conn, "bad", "finished", 0.2, make_replay(structured_timeouts=2))
    summary = run_timeout_patrol(dry_run=True)
    assert summary["dry_run"] is True
    assert [c["room_id"] for c in summary["contaminated"]] == ["bad"]
    assert summary["deleted_count"] == 0
    with db.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM games")).scalar() == 1


def test_patrol_threshold_and_cascade(db):
    with db.begin() as conn:
        _seed_game(conn, "bad", "finished", 0.2, make_replay(structured_timeouts=2))
        _seed_game(conn, "border", "finished", 0.15, make_replay(n_prompts=20, structured_timeouts=3))
        _seed_game(conn, "clean", "finished", 0.0, make_replay())
        _seed_game(conn, "running", "running", 0.5, make_replay(structured_timeouts=5))
        _seed_game(conn, "legacy", "finished", 0.2, make_replay(legacy_timeouts=2))
        _seed_participation(conn, "bad", 1, "wolf", True, 10, 5, 3, 2)
        _seed_participation(conn, "clean", 1, "villager", False, -5, 4, 1, 1)
        _seed_participation(conn, "bad", 2, "seer", True, 10, 3, 2, 1)
        conn.execute(text("INSERT INTO conjugate_agent_participations (room_id, conjugate_agent_id) VALUES ('bad', 7)"))
        conn.execute(text("INSERT INTO conjugate_agent_participations (room_id, conjugate_agent_id) VALUES ('clean', 7)"))
        conn.execute(text("INSERT INTO evolution_game_archive (game_id, room_id) VALUES ('bad', 'bad')"))
        conn.execute(text("INSERT INTO evolution_game_archive (game_id, room_id) VALUES ('clean', 'clean')"))
        for aid in (1, 2, 3):
            conn.execute(text("INSERT INTO provider_agent_stats (provider_agent_id, games_played, score) VALUES (:a, 99, 999)"), {"a": aid})

    summary = run_timeout_patrol()

    assert sorted(summary["deleted_rooms"]) == ["bad", "legacy"]
    assert summary["deleted_count"] == 2
    with db.connect() as conn:
        remaining_rooms = {r[0] for r in conn.execute(text("SELECT room_id FROM rooms"))}
        assert remaining_rooms == {"border", "clean", "running"}
        assert conn.execute(text("SELECT COUNT(*) FROM games WHERE room_id IN ('bad','legacy')")).scalar() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM provider_game_participations WHERE room_id='bad'")).scalar() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM conjugate_agent_participations WHERE room_id='bad'")).scalar() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM evolution_game_archive WHERE room_id='bad'")).scalar() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM evolution_game_archive WHERE room_id='clean'")).scalar() == 1

        stats = {r[0]: dict(r._mapping) for r in conn.execute(text("SELECT * FROM provider_agent_stats"))}
        assert stats[1]["games_played"] == 1
        assert stats[1]["wins"] == 0
        assert stats[1]["score"] == INITIAL_SCORE - 5
        assert stats[1]["games_as_villager"] == 1
        assert stats[1]["total_votes"] == 4
        assert stats[1]["total_correct_votes"] == 1
        assert stats[2]["games_played"] == 0
        assert stats[2]["score"] == INITIAL_SCORE
        assert stats[3]["games_played"] == 0
        assert stats[3]["score"] == INITIAL_SCORE


def test_patrol_no_candidates(db):
    with db.begin() as conn:
        _seed_game(conn, "clean", "finished", 0.0, make_replay())
    summary = run_timeout_patrol()
    assert summary["deleted_count"] == 0
    assert summary["contaminated"] == []