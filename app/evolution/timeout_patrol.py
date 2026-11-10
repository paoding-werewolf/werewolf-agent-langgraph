"""evolution/timeout_patrol.py — 策展人 timeout 污染巡检。

污染率只统计 timeout 类错误：结构化 errors 中 type=="timeout"，以及旧回放里
message 恰为 "TIMEOUT" 的事件（不含 ERROR/EXCEPTION 等其他关键词）。
污染率超过阈值的已完赛对局会被连同参与记录、进化档案一并删除，
并从剩余参与记录重建 provider_agent_stats，保证统计公允。
"""
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from sqlalchemy import bindparam, text

from evolution.db import get_session

logger = logging.getLogger(__name__)

# 与 backend/agentproviders/models.py 的 INITIAL_SCORE 保持一致
INITIAL_SCORE = 300
DEFAULT_TIMEOUT_RATE_THRESHOLD = 0.15

_WOLF_ROLES = ("wolf", "wolf_king")
_GOD_ROLES = ("seer", "witch", "guard", "hunter")


def timeout_metrics_from_record(record_json: Any) -> Dict[str, Any]:
    """从持久化回放计算 timeout 污染率。

    分母与 backend compute_error_metrics 一致：kind=="prompt" 事件数
    （按 relations 数加权），无 prompt 时回退为全部事件数。
    """
    if isinstance(record_json, str):
        try:
            record_json = json.loads(record_json)
        except (json.JSONDecodeError, TypeError):
            record_json = {}
    timeline = record_json.get("timeline") if isinstance(record_json, dict) else None
    events = [e for e in (timeline or []) if isinstance(e, dict)]

    timeout_count = 0
    action_count = 0
    for event in events:
        if event.get("kind") == "prompt":
            relations = event.get("relations") or []
            action_count += max(1, len(relations))

        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        errors = data.get("errors") if isinstance(data.get("errors"), list) else []
        structured = [e for e in errors if isinstance(e, dict)]
        if structured:
            timeout_count += sum(
                1 for e in structured if str(e.get("type") or "") == "timeout"
            )
        elif str(event.get("message") or "").strip().upper() == "TIMEOUT":
            timeout_count += 1

    denominator = action_count or len(events)
    return {
        "timeout_count": timeout_count,
        "action_count": action_count,
        "timeout_rate": (timeout_count / denominator) if denominator else 0.0,
    }


def _rebuild_provider_stats(session) -> int:
    """从剩余参与记录全量重建 provider_agent_stats（含清零无参与记录的 agent）。"""
    session.execute(text("""
        UPDATE provider_agent_stats
        SET score = :initial_score, games_played = 0, wins = 0, win_rate = 0.0,
            games_as_wolf = 0, wins_as_wolf = 0,
            games_as_god = 0, wins_as_god = 0,
            games_as_villager = 0, wins_as_villager = 0,
            survived_count = 0, mvp_count = 0,
            total_votes = 0, total_correct_votes = 0, total_survived_rounds = 0,
            last_game_at = NULL
    """), {"initial_score": INITIAL_SCORE})

    rows = session.execute(text("""
        SELECT provider_agent_id,
               COUNT(*) AS games,
               SUM(is_winner) AS wins,
               SUM(survived) AS survived,
               SUM(score_delta) AS score_delta_sum,
               SUM(votes_cast) AS votes,
               SUM(correct_votes) AS correct_votes,
               SUM(survived_rounds) AS survived_rounds,
               SUM(CASE WHEN role IN :wolf_roles THEN 1 ELSE 0 END) AS games_as_wolf,
               SUM(CASE WHEN role IN :wolf_roles AND is_winner THEN 1 ELSE 0 END) AS wins_as_wolf,
               SUM(CASE WHEN role IN :god_roles THEN 1 ELSE 0 END) AS games_as_god,
               SUM(CASE WHEN role IN :god_roles AND is_winner THEN 1 ELSE 0 END) AS wins_as_god,
               SUM(CASE WHEN role = 'villager' THEN 1 ELSE 0 END) AS games_as_villager,
               SUM(CASE WHEN role = 'villager' AND is_winner THEN 1 ELSE 0 END) AS wins_as_villager,
               MAX(created_at) AS last_game_at
        FROM provider_game_participations
        GROUP BY provider_agent_id
    """).bindparams(
        bindparam("wolf_roles", expanding=True),
        bindparam("god_roles", expanding=True),
    ), {"wolf_roles": list(_WOLF_ROLES), "god_roles": list(_GOD_ROLES)}).mappings().all()

    for row in rows:
        games = int(row["games"] or 0)
        wins = int(row["wins"] or 0)
        session.execute(text("""
            UPDATE provider_agent_stats
            SET score = :score, games_played = :games, wins = :wins, win_rate = :win_rate,
                games_as_wolf = :games_as_wolf, wins_as_wolf = :wins_as_wolf,
                games_as_god = :games_as_god, wins_as_god = :wins_as_god,
                games_as_villager = :games_as_villager, wins_as_villager = :wins_as_villager,
                survived_count = :survived,
                total_votes = :votes, total_correct_votes = :correct_votes,
                total_survived_rounds = :survived_rounds, last_game_at = :last_game_at
            WHERE provider_agent_id = :agent_id
        """), {
            "score": INITIAL_SCORE + int(row["score_delta_sum"] or 0),
            "games": games,
            "wins": wins,
            "win_rate": wins / games if games else 0.0,
            "games_as_wolf": int(row["games_as_wolf"] or 0),
            "wins_as_wolf": int(row["wins_as_wolf"] or 0),
            "games_as_god": int(row["games_as_god"] or 0),
            "wins_as_god": int(row["wins_as_god"] or 0),
            "games_as_villager": int(row["games_as_villager"] or 0),
            "wins_as_villager": int(row["wins_as_villager"] or 0),
            "survived": int(row["survived"] or 0),
            "votes": int(row["votes"] or 0),
            "correct_votes": int(row["correct_votes"] or 0),
            "survived_rounds": int(row["survived_rounds"] or 0),
            "last_game_at": row["last_game_at"],
            "agent_id": row["provider_agent_id"],
        })
    return len(rows)


def run_timeout_patrol(
    threshold: float = DEFAULT_TIMEOUT_RATE_THRESHOLD,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """巡检并清理 timeout 污染率超过阈值的已完赛对局。

    候选预筛用 error_rate（timeout 数 ≤ 总错误数，故 timeout_rate > threshold
    必然 error_rate ≥ threshold；减去 0.001 容差覆盖 decimal(8,4) 舍入）。
    """
    session = get_session()
    summary: Dict[str, Any] = {
        "threshold": threshold,
        "dry_run": dry_run,
        "scanned": 0,
        "candidates": 0,
        "contaminated": [],
        "deleted_count": 0,
        "deleted_rooms": [],
        "stats_agents_updated": 0,
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        rows = session.execute(text("""
            SELECT g.room_id, g.events_json
            FROM games g JOIN rooms r ON g.room_id = r.room_id
            WHERE g.error_rate >= :min_rate AND r.status = 'finished'
        """), {"min_rate": max(0.0, threshold - 0.001)}).all()
        summary["scanned"] = len(rows)

        contaminated: List[Dict[str, Any]] = []
        for room_id, events_json in rows:
            metrics = timeout_metrics_from_record(events_json)
            if metrics["timeout_rate"] > threshold:
                contaminated.append({"room_id": room_id, **metrics})
        summary["candidates"] = len(rows)
        summary["contaminated"] = contaminated

        if dry_run or not contaminated:
            return summary

        for item in contaminated:
            room_id = item["room_id"]
            session.execute(text("DELETE FROM provider_game_participations WHERE room_id = :rid"), {"rid": room_id})
            session.execute(text("DELETE FROM conjugate_agent_participations WHERE room_id = :rid"), {"rid": room_id})
            session.execute(text("DELETE FROM evolution_game_archive WHERE room_id = :rid"), {"rid": room_id})
            session.execute(text("DELETE FROM games WHERE room_id = :rid"), {"rid": room_id})
            session.execute(text("DELETE FROM rooms WHERE room_id = :rid"), {"rid": room_id})
            summary["deleted_rooms"].append(room_id)
            logger.info(
                "[TimeoutPatrol] 清理污染对局 %s (timeout_rate=%.3f, timeouts=%d)",
                room_id, item["timeout_rate"], item["timeout_count"],
            )

        summary["stats_agents_updated"] = _rebuild_provider_stats(session)
        session.commit()
        summary["deleted_count"] = len(summary["deleted_rooms"])
        logger.info(
            "[TimeoutPatrol] 巡检完成: scanned=%d, deleted=%d, stats_rebuilt=%d",
            summary["scanned"], summary["deleted_count"], summary["stats_agents_updated"],
        )
        return summary
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()