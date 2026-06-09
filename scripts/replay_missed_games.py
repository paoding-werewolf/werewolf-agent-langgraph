#!/usr/bin/env python3
"""replay_missed_games.py — 用历史 transcript 重放反思管道，补沉淀故障期间遗漏的对局。

背景：2026-06-03 一次部署后，外部 agent 的内存 session 丢失，game_over 命中
"Session not found"，导致 _run_post_game_pipeline 从未执行——约 88 场全外部 agent
对局未被反思/聚类/确认。事后补救只写了 minimal archive（仅让"对局总数"+1）。

本脚本从 games.events_json（timeline + thoughts）重建每个席位可读 trace，对每局每个
角色席位运行真实的 ReflectionEngine → BufferPool.ingest，最后统一跑一次聚类 + 确认，
让面板的"聚类中/已确认/策略数"真实增长，并把 minimal archive 升级为完整归档。

必须在 agent 容器内运行（/app 工作目录，复用其 LLM + MySQL 配置）：
  docker exec werewolf-agent-langgraph python scripts/replay_missed_games.py --limit 1 --dry-run
  docker exec werewolf-agent-langgraph python scripts/replay_missed_games.py --limit 1
  docker exec werewolf-agent-langgraph python scripts/replay_missed_games.py --all

幂等：已重放的对局会在 archive.payload_json.replayed=true 上做标记并跳过。
"""
import argparse
import json
import logging
import sys
from datetime import datetime, timezone

# 确保以 /app 为根可导入 evolution / agents / memory
sys.path.insert(0, "/app")

from sqlalchemy import text

from evolution.config import load_config
from evolution.db import get_session
from evolution.reflection_engine import ReflectionEngine
from evolution.buffer_pool import BufferPool
from evolution.clustering import SuggestionClusterer
from evolution.confirmation import ConfirmationJudge
from evolution.version_manager import VersionManager
from evolution.models import EvolutionGameArchive
from memory.game_archive import save_game

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("replay")

WOLF_ROLES = {"wolf", "wolf_king"}
CUTOFF = "2026-06-03 06:00:00"  # 故障起始（buffer 最后一条 6/3 05:32 之后）


def team_of(role: str) -> str:
    return "wolf" if role in WOLF_ROLES else "good"


def strategy_role(role: str) -> str:
    return "wolf" if role in WOLF_ROLES else role


def build_versions_used(vm: VersionManager, role: str) -> dict:
    role_key = strategy_role(role)
    return {
        skill["name"]: vm.get_version_for_game(skill["name"])
        for skill in vm.loader.load_index()
        if strategy_role(str(skill.get("role") or "common")) in (role_key, "common")
    }


def fetch_missed_games(session, limit=None, only_room=None):
    """返回需要重放的对局列表：故障期 finished 房间，且尚未被真实重放。"""
    sql = """
        SELECT r.room_id, r.winner, r.finished_at,
               g.events_json, g.players_json, g.round_count
        FROM rooms r JOIN games g ON r.room_id = g.room_id
        WHERE r.status = 'finished'
          AND r.finished_at >= :cutoff
          AND r.winner IN ('wolf','good')
          AND g.events_json IS NOT NULL
    """
    params = {"cutoff": CUTOFF}
    if only_room:
        sql += " AND r.room_id = :room"
        params["room"] = only_room
    sql += " ORDER BY r.finished_at ASC"
    rows = session.execute(text(sql), params).mappings().all()

    # 过滤掉已重放的（archive.payload_json.replayed == true）
    out = []
    for r in rows:
        arc = session.query(EvolutionGameArchive).filter_by(game_id=r["room_id"]).first()
        if arc and isinstance(arc.payload_json, dict) and arc.payload_json.get("replayed"):
            continue
        out.append(dict(r))
        if limit and len(out) >= limit:
            break
    return out


def _loads(v):
    if v is None:
        return None
    return json.loads(v) if isinstance(v, str) else v


def seat_roles(players_json) -> dict:
    """players_json -> {seat_id(str): role}."""
    players = _loads(players_json) or []
    roles = {}
    for p in players:
        if isinstance(p, dict) and p.get("id") is not None:
            roles[str(p["id"])] = p.get("role", "")
    return roles


def build_seat_trace(events_json, seat_id: str) -> tuple[str, int]:
    """从 timeline + thoughts 为指定席位构建可读 trace，返回 (trace_text, max_day)。

    可见性：public 事件全可见；private 事件仅当该席位在 visibility.seat_ids 内可见。
    额外附上该席位自己的局内思考（thoughts）。
    """
    ev = _loads(events_json) or {}
    timeline = ev.get("timeline", []) or []
    thoughts = ev.get("thoughts", []) or []

    lines = []
    cur_day = None
    max_day = 1
    for item in timeline:
        vis = item.get("visibility", {}) or {}
        scope = vis.get("scope", "public")
        seat_ids = [str(s) for s in (vis.get("seat_ids") or [])]
        if scope != "public" and seat_id not in seat_ids:
            continue

        state_after = item.get("state_after", {}) or {}
        day = state_after.get("day", cur_day or 1)
        try:
            max_day = max(max_day, int(day))
        except (TypeError, ValueError):
            pass
        if day != cur_day:
            cur_day = day
            lines.append(f"\n--- 第 {day} 天 ---")

        typ = item.get("type", "")
        msg = (item.get("message", "") or "").strip()
        rel_parts = []
        for rel in (item.get("relations") or []):
            actor = rel.get("actor_id")
            target = rel.get("target_id")
            action = rel.get("action")
            if actor and target:
                rel_parts.append(f"{actor}→{target}" + (f"({action})" if action else ""))
            elif actor and action:
                rel_parts.append(f"{actor}({action})")
        rel_str = "  [" + ", ".join(rel_parts) + "]" if rel_parts else ""
        priv = "" if scope == "public" else "（私密）"
        lines.append(f"[{typ}{priv}] {msg}{rel_str}")

    trace = "\n".join(lines)

    # 附上该席位自己的思考
    my_thoughts = [t for t in thoughts if str(t.get("seat_id")) == seat_id]
    if my_thoughts:
        tlines = ["\n\n## 我（{}号）的局内思考".format(seat_id)]
        for t in my_thoughts:
            d = t.get("day", "?")
            ph = t.get("phase", "")
            m = (t.get("message", "") or "").strip()
            tlines.append(f"[第{d}天 {ph}] {m}")
        trace += "\n".join(tlines)

    return trace, max_day


def select_seats(roles: dict, mode: str) -> list[str]:
    """选择要反思的席位。distinct=每个不同角色取一个席位；all=全部 12 席。"""
    if mode == "all":
        return list(roles.keys())
    seen = {}
    for seat, role in roles.items():
        if role and role not in seen:
            seen[role] = seat
    return list(seen.values())


def mark_replayed(session, game_id: str, n_reflections: int):
    arc = session.query(EvolutionGameArchive).filter_by(game_id=game_id).first()
    if not arc:
        return
    payload = dict(arc.payload_json) if isinstance(arc.payload_json, dict) else {}
    payload["replayed"] = True
    payload["replayed_at"] = datetime.now(timezone.utc).isoformat()
    payload["replay_reflections"] = n_reflections
    arc.payload_json = payload
    session.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="最多处理几局对局")
    ap.add_argument("--all", action="store_true", help="处理全部遗漏对局")
    ap.add_argument("--room", type=str, default=None, help="只处理指定 room_id")
    ap.add_argument("--mode", choices=["distinct", "all"], default="distinct",
                    help="distinct=每个不同角色一个席位（默认）；all=全部 12 席")
    ap.add_argument("--dry-run", action="store_true", help="只构建 trace 并统计，不调 LLM、不写库")
    args = ap.parse_args()

    if not args.all and not args.limit and not args.room:
        ap.error("必须指定 --limit N / --room ID / --all 之一")

    cfg = load_config()
    if not cfg.enabled and not args.dry_run:
        logger.error("Evolution 未启用（cfg.enabled=False），中止。")
        return

    session = get_session()
    games = fetch_missed_games(session, limit=(None if args.all else args.limit), only_room=args.room)
    logger.info(f"待重放对局数: {len(games)}  (mode={args.mode}, dry_run={args.dry_run})")
    if not games:
        logger.info("没有需要重放的对局。")
        return

    pool = BufferPool(cfg)
    vm = VersionManager(cfg)
    engine = ReflectionEngine(cfg)

    total_reflections = 0
    total_ingested = 0
    for gi, g in enumerate(games, 1):
        room_id = g["room_id"]
        winner = g["winner"]
        roles = seat_roles(g["players_json"])
        seats = select_seats(roles, args.mode)
        logger.info(f"[{gi}/{len(games)}] room={room_id} winner={winner} "
                    f"seats_to_reflect={len(seats)} (total_seats={len(roles)})")

        n_ref = 0
        rep_role = None
        rep_day = 1
        rep_versions_used = {}
        for seat in seats:
            role = roles.get(seat, "")
            if not role:
                continue
            result = "won" if team_of(role) == winner else "lost"
            trace, max_day = build_seat_trace(g["events_json"], seat)
            versions_used = build_versions_used(vm, role)
            if rep_role is None:
                rep_role, rep_day = role, max_day
                rep_versions_used = versions_used
            if args.dry_run:
                logger.info(f"    seat={seat} role={role} result={result} "
                            f"trace_len={len(trace)} day={max_day}")
                n_ref += 1
                continue

            current_strategies = vm.format_skills_for_prompt(role, "", versions_used)
            reflection = engine.reflect(
                game_id=room_id,
                my_role=role,
                my_seat=seat,
                result=result,
                game_trace=trace,
                in_game_flags=[],
                current_strategies=current_strategies,
                working_memory_text="",
            )
            n_ref += 1
            if reflection:
                pool.ingest(reflection)
                total_ingested += 1

        total_reflections += n_ref

        if not args.dry_run and rep_role:
            # 升级该局归档为完整归档（一局一行，保留代表角色）
            rep_result = "won" if team_of(rep_role) == winner else "lost"
            save_game(
                game_id=room_id,
                my_role=rep_role,
                result=rep_result,
                day_count=rep_day,
                scene_tags={"role": rep_role, "result": rep_result, "wolf_aggression": "unknown"},
                reflection_report="replayed from transcript",
                full_trace="",
                strategies_used=list(rep_versions_used.keys()),
                versions_used=rep_versions_used,
            )
            mark_replayed(session, room_id, n_ref)

    logger.info(f"反思完成: 对局={len(games)} 反思次数={total_reflections} 入池={total_ingested}")

    if args.dry_run:
        logger.info("dry-run 结束，未触发聚类/确认。")
        return

    # 统一跑一次聚类 + 确认（debounce 设计：批量比每条都跑更省 LLM）
    logger.info("运行聚类 process_pending ...")
    clusterer = SuggestionClusterer(cfg, pool)
    clusters = clusterer.process_pending()
    logger.info(f"聚类产出: {len(clusters)} 个簇")

    logger.info("运行确认 check_all_clusters ...")
    judge = ConfirmationJudge(cfg, pool, vm)
    confirmed = judge.check_all_clusters()
    logger.info(f"确认产出: {len(confirmed)} 项")

    status = pool.get_status()
    logger.info(f"缓冲池状态: pending={status.get('pending_count')} "
                f"cluster={status.get('cluster_count')} confirmed={status.get('confirmed_count')}")
    logger.info("全部完成。")


if __name__ == "__main__":
    main()
