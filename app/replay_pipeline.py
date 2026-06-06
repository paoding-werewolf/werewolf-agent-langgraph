#!/usr/bin/env python3
"""replay_pipeline.py — 从 DB 重建 state，对指定 room 重跑 post-game pipeline。

用法:
    python replay_pipeline.py <room_id> [--seat <seat_id>] [--dry-run]
"""
import asyncio
import json
import logging
import sys
import os
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import asdict

# 确保 app/ 在 import path 上
sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("replay_pipeline")


def _load_game_from_db(room_id: str) -> dict:
    """从 DB 加载 room + game 数据。"""
    from evolution.db import engine
    from sqlalchemy import text

    with engine.connect() as conn:
        row = conn.execute(text("SELECT room_id, status, winner, seats_json FROM rooms WHERE room_id=:rid"), {"rid": room_id}).mappings().first()
        if not row:
            raise ValueError(f"Room {room_id} not found")
        room = dict(row)

        grow = conn.execute(text("SELECT players_json, events_json, round_count FROM games WHERE room_id=:rid"), {"rid": room_id}).mappings().first()
        if not grow:
            raise ValueError(f"Game data for room {room_id} not found")
        game = dict(grow)

    return {"room": room, "game": game}


def _build_all_roles(players_json: list) -> dict:
    return {str(p["id"]): p["role"] for p in players_json}


def _determine_result(my_role: str, winner: str) -> str:
    wolf_roles = {"wolf", "wolf_king"}
    if winner == "wolf":
        return "won" if my_role in wolf_roles else "lost"
    return "lost" if my_role in wolf_roles else "won"


def _timeline_to_events(timeline: list) -> list:
    """将 DB timeline 转换为 format_game_trace 期望的 events 格式。"""
    events = []
    for t in timeline:
        day = (t.get("state_after") or {}).get("day", 1)
        status = t.get("type", "")
        content = t.get("message", "")
        traces = []
        for r in t.get("relations", []):
            traces.append({
                "from": r.get("actor_id", ""),
                "to": r.get("target_id", ""),
                "action": r.get("action", "") or "",
            })
        events.append({"round": day, "status": status, "content": content, "traces": traces})
    return events


def _build_last_thought(thoughts: list, seat_id: str) -> str:
    seat_thoughts = [t for t in thoughts if str(t.get("seat_id", "")) == str(seat_id)]
    if seat_thoughts:
        return seat_thoughts[-1].get("message", "")
    return ""


def _extract_player_behavior(state: dict, player_id: str) -> str:
    """从 events 中提取玩家行为摘要（复用 main_ws 逻辑）。"""
    events = state.get("events", [])
    behaviors = []
    for event in events:
        content = event.get("content", "")
        traces = event.get("traces", [])
        status = event.get("status", "")
        round_num = event.get("round", 1)

        for t in traces:
            if t.get("from") == player_id or t.get("to") == player_id:
                action_desc = f"R{round_num} {status}: {t.get('from','')}->{t.get('to','')}({t.get('action','')})"
                behaviors.append(action_desc)

        if status == "discussion" and player_id in content:
            behaviors.append(f"R{round_num} speech: {content[:100]}")

        if status in ("vote", "vote_result") and player_id in content:
            behaviors.append(f"R{round_num} vote: {content}")

    return "\n".join(behaviors[:20]) if behaviors else ""


def _build_state(room: dict, game: dict, seat_id: str, my_role: str) -> dict:
    players_json = json.loads(game["players_json"]) if isinstance(game["players_json"], str) else game["players_json"]
    events_json = json.loads(game["events_json"]) if isinstance(game["events_json"], str) else game["events_json"]

    timeline = events_json.get("timeline", [])
    thoughts = events_json.get("thoughts", [])

    events = _timeline_to_events(timeline)
    players = {str(p["id"]): p for p in players_json}

    return {
        "events": events,
        "players": players,
        "in_game_flags": [],
        "my_role": my_role,
        "phase": "finished",
        "working_memory": None,
        "room_id": room["room_id"],
        "me_id": seat_id,
        "day": game.get("round_count", 1),
        "strategies_used": [],
        "last_thought": _build_last_thought(thoughts, seat_id),
        "versions_used": {},
    }


async def replay_pipeline(state: dict, result: str, winner_role: str,
                          all_roles: dict, seat_id: str, dry_run: bool = False):
    """对单个席位重跑完整管道（直接调用各模块，不通过 main_ws）。"""
    import yaml
    from evolution.config import load_config
    from evolution.reflection_engine import ReflectionEngine, format_game_trace
    from evolution.buffer_pool import BufferPool
    from evolution.version_manager import VersionManager
    from memory.game_archive import save_game, record_strategy_gap
    from memory.self_model import update_self_model
    from memory.opponent_model import update_opponent_from_game
    from agents.llm_caller import llm

    session_id = f"replay_{state['room_id']}_{seat_id}"
    logger.info(f"[seat {seat_id}] Starting replay: role={state['my_role']}, result={result}")

    if dry_run:
        logger.info(f"[seat {seat_id}] DRY-RUN — skipping execution")
        return

    try:
        cfg = load_config()
        if not cfg.enabled:
            logger.info(f"[seat {seat_id}] Evolution disabled, skipping")
            return

        # 1. Format trace
        game_trace = format_game_trace(state.get("events", []), state.get("players", {}))

        # 2. In-game flags (重建时不具备)
        flags = list(state.get("in_game_flags", []))

        # 3. Load current strategies
        vm = VersionManager(cfg)
        current_strategies = vm.format_skills_for_prompt(state["my_role"], state.get("phase", ""))

        # 3.1 Initialize buffer pool
        pool = BufferPool(cfg)

        # 4. Reflection
        engine = ReflectionEngine(cfg)
        reflection = engine.reflect(
            game_id=state.get("room_id", "unknown"),
            my_role=state["my_role"],
            my_seat=state["me_id"],
            result=result,
            game_trace=game_trace,
            in_game_flags=flags,
            current_strategies=current_strategies,
            working_memory_text="",
        )

        if reflection:
            # 5. Ingest to buffer
            pool.ingest(reflection)
            logger.info(f"[seat {seat_id}] Reflection ingested: target_skill={reflection.suggestion.target_skill}")

            # 6+7. 聚类/确认/过期 — 直接跑（不需要去抖动，replay 场景顺序执行）
            from evolution.clustering import SuggestionClusterer
            from evolution.confirmation import ConfirmationJudge
            from evolution.curator import Curator

            clusterer = SuggestionClusterer(cfg, pool)
            clusterer.process_pending()

            judge = ConfirmationJudge(cfg, pool, vm)
            judge.check_all_clusters()

            pool.expire_old_suggestions()

            curator = Curator(cfg)
            curator._save_state({"last_game_end_at": datetime.now(timezone.utc).isoformat()})
            if curator.should_run(is_game_in_progress=False):
                try:
                    summary = curator.run()
                    logger.info(f"[seat {seat_id}] Curator: {summary}")
                except Exception as e:
                    logger.warning(f"[seat {seat_id}] Curator failed: {e}")

            # 8. Record strategy_gap
            if reflection.suggestion.match_level in ("low", "strategy_gap"):
                record_strategy_gap(
                    reflection.game_id,
                    f"{reflection.scene_tags.role}_{reflection.scene_tags.critical_phase}"
                )

            # 9. Archive game
            save_game(
                game_id=reflection.game_id,
                my_role=reflection.my_role,
                result=result,
                day_count=state.get("day", 1),
                scene_tags={
                    "role": reflection.scene_tags.role,
                    "result": reflection.scene_tags.result,
                    "wolf_aggression": reflection.scene_tags.wolf_aggression,
                },
                reflection_report=yaml.dump(asdict(reflection), allow_unicode=True, default_flow_style=False),
                full_trace=game_trace,
                strategies_used=state.get("strategies_used", []),
            )
            logger.info(f"[seat {seat_id}] Game archived")

        # 10. Update self model
        update_self_model(
            my_role=state["my_role"],
            result=result,
            key_decisions=state.get("last_thought", ""),
            llm_caller=llm,
        )
        logger.info(f"[seat {seat_id}] Self model updated")

        # 10.1 Update opponent models
        my_seat = state.get("me_id", "")
        for player_id, player_role in (all_roles or {}).items():
            if player_id == my_seat:
                continue
            behavior_summary = _extract_player_behavior(state, player_id)
            if behavior_summary:
                update_opponent_from_game(
                    player_id=player_id,
                    role=player_role,
                    behavior_summary=behavior_summary,
                    llm_caller=llm,
                )
        logger.info(f"[seat {seat_id}] Opponent models updated")

        logger.info(f"[seat {seat_id}] Pipeline complete: result={result}")
    except Exception:
        logger.exception(f"[seat {seat_id}] Pipeline failed")
        raise


async def main():
    args = sys.argv[1:]
    if not args:
        print("Usage: python replay_pipeline.py <room_id> [--seat <seat_id>] [--dry-run]")
        sys.exit(1)

    room_id = args[0]
    target_seat = None
    dry_run = False

    i = 1
    while i < len(args):
        if args[i] == "--seat" and i + 1 < len(args):
            target_seat = args[i + 1]
            i += 2
        elif args[i] == "--dry-run":
            dry_run = True
            i += 1
        else:
            i += 1

    # 安装日志落盘 handler（和线上环境一致）
    from evolution.log_handler import install_evolution_log_handler
    install_evolution_log_handler()

    data = _load_game_from_db(room_id)
    room = data["room"]
    game = data["game"]

    players_json = json.loads(game["players_json"]) if isinstance(game["players_json"], str) else game["players_json"]
    all_roles = _build_all_roles(players_json)
    winner_role = room.get("winner", "wolf")

    logger.info(f"Room {room_id}: winner={winner_role}, players={len(players_json)}, dry_run={dry_run}")

    for player in players_json:
        seat_id = str(player["id"])
        my_role = player["role"]
        result = _determine_result(my_role, winner_role)

        if target_seat and seat_id != target_seat:
            continue

        state = _build_state(room, game, seat_id, my_role)
        await replay_pipeline(state, result, winner_role, all_roles, seat_id, dry_run)

    logger.info("=== Replay complete ===")


if __name__ == "__main__":
    asyncio.run(main())
