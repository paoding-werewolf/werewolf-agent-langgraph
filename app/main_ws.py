"""
WebSocket Agent Service — 与 WebSocketAgentClient 配套的落地实现。

复用全部现有 HTTP 版的 agent logic (agent_graph / state / prompt_builder / llm_caller),
仅将 transport 层从 HTTP FastAPI 改为 WebSocket 长连接。

会话模型 (session 化, 支持单个逻辑 agent 多实例):
  init 由服务端铸造唯一 session_id 并下发; 之后的 perceive/act 都携带该 session_id.
  SessionStore 以 session_id 为键, 每个实例一份独立状态, 因此每次 init = 一条互相隔离
  的会话, 即使多个实例复用同一 agent_id 也互不串台. 空闲超过 TTL (默认 2h, 可配
  SESSION_TTL_SECONDS) 的 session 会被后台清理.

  调试端点 (同端口 HTTP): GET /debug/prompts (JSON), GET /debug/view (HTML),
  均支持 ?session_id= 过滤.

协议:
  客户端 → 服务端:
    init      {"type": "init", "req_id": "...", "agent_id": "...", "role": "...", "teammates": [...]}
    perceive  {"type": "perceive", "req_id": "...", "session_id": "...", "status": "...", ...}
    act       {"type": "act", "req_id": "...", "session_id": "...", "status": "...", ...}
  服务端 → 客户端:
    init_ok     {"type": "init_ok", "req_id": "...", "session_id": "...", "agent_id": "..."}
    perceive_ok {"type": "perceive_ok", "req_id": "..."}
    thought     {"type": "thought", "session_id": "...", "agent_id": "...", "round": N,
                 "phase": "...", "content": "...", "seq": K}   # 思考过程回传, 可随时多次下发
    act_result  {"type": "act_result", "req_id": "...", "result": "...", "target": "..."}
    error       {"type": "error", "req_id": "...", "detail": "..."}

向后兼容: perceive/act 若未带 session_id, 回退使用 agent_id 作为 thread_id.
"""

import asyncio
import difflib
import email.utils
import http
import json
import logging
import os
import signal
import sys
import uuid
from collections import defaultdict
from urllib.parse import urlsplit, parse_qs

import websockets
from websockets.asyncio.server import ServerConnection
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

try:
    from aiohttp import web as aiohttp_web
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

# 复用现有的 agent 核心逻辑
from agents.agent_graph import run_perceive, run_act
from agents.session_store import SessionStore
from agents.state import make_initial_state
from utils.prompt_logger import prompt_logger
from utils.debug_view import render_prompts_html

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ws_agent_service")

# 将 evolution.* WARNING+ 日志落盘到 MySQL
from evolution.log_handler import install_evolution_log_handler
install_evolution_log_handler()


# 每个 session_id 一份独立状态; 空闲超过 TTL (默认 2h, 可配 SESSION_TTL_SECONDS) 后清理.
store = SessionStore()


def _json_error(detail: str, status: int = 400):
    return aiohttp_web.json_response({"detail": detail}, status=status)


def _safe_path_component(value: str) -> str:
    if not value or ".." in value or "/" in value or "\\" in value:
        raise ValueError(f"Invalid path component: {value!r}")
    return value


def _provider_public_urls() -> tuple[str, str]:
    """Return externally reachable HTTP / WS base URLs for provider consumers."""
    http_url = os.getenv("PROVIDER_PUBLIC_HTTP_URL", "").strip().rstrip("/")
    ws_url = os.getenv("PROVIDER_PUBLIC_WS_URL", "").strip().rstrip("/")
    if http_url and ws_url:
        return http_url, ws_url

    public_host = os.getenv("PROVIDER_PUBLIC_HOST", "").strip() or "172.17.0.1"
    http_port = int(os.getenv("PROVIDER_PUBLIC_HTTP_PORT", "7860"))
    ws_port = int(os.getenv("PROVIDER_PUBLIC_WS_PORT", "7861"))
    return f"http://{public_host}:{http_port}", f"ws://{public_host}:{ws_port}"


def _build_versions_used(role: str, external_agent_id: str | None = None) -> dict:
    """Build the concrete skill-version map for one game session.

    Provider mode currently exposes:
    - a default participant using each skill's current selection logic
    - per-skill candidate participants that override one skill to a fixed version
    """
    from evolution.config import load_config
    from evolution.version_manager import VersionManager

    cfg = load_config()
    vm = VersionManager(cfg)
    index = vm.loader.load_index()
    versions_used = {}
    for skill in index:
        if skill.get("role") in (role, "common"):
            versions_used[skill["name"]] = vm.loader.get_version_for_game(skill["name"])

    if external_agent_id:
        parts = external_agent_id.split(":")
        if len(parts) == 4 and parts[0] == "skill":
            _, skill_name, version, agent_role = parts
            if agent_role == role and skill_name in versions_used:
                versions_used[skill_name] = version

    return versions_used


def _list_provider_agents() -> list[dict]:
    """Expose version-addressable participants for the orchestration layer."""
    from evolution.config import load_config
    from evolution.version_manager import VersionManager

    _http_base, ws_base = _provider_public_urls()
    cfg = load_config()
    vm = VersionManager(cfg)
    try:
        index = vm.loader.load_index()
    except Exception:
        logger.exception("Failed to load evolution skill index for provider agents")
        return [
            {
                "external_agent_id": "default:common",
                "agent_name": "DefaultAgent",
                "client_type": "ws",
                "client_url": ws_base,
                "model_name": "werewolf-agent-langgraph",
                "version": "default",
                "health": "available",
                "status": "available",
                "metadata": {
                    "mode": "default",
                    "role_scope": "all",
                    "skill_count": 0,
                    "partial": True,
                },
            }
        ]
    grouped: dict[str, list[dict]] = defaultdict(list)
    for skill in index:
        if not isinstance(skill, dict):
            logger.warning("Skip non-dict skill index entry: %r", skill)
            continue
        skill_name = str(skill.get("name") or "").strip()
        if not skill_name:
            logger.warning("Skip skill index entry without name: %r", skill)
            continue
        role = str(skill.get("role") or "common").strip() or "common"
        grouped[role].append(skill)

    result = [
        {
            "external_agent_id": "default:common",
            "agent_name": "DefaultAgent",
            "client_type": "ws",
            "client_url": ws_base,
            "model_name": "werewolf-agent-langgraph",
            "version": "default",
            "health": "available",
            "status": "available",
            "metadata": {
                "mode": "default",
                "role_scope": "all",
                "skill_count": len(index),
            },
        }
    ]

    for role, skills in grouped.items():
        if role == "common":
            continue
        for skill in skills:
            skill_name = str(skill.get("name") or "").strip()
            if not skill_name:
                continue
            try:
                meta = vm.loader._load_versions_meta(skill_name) or {}
            except Exception:
                logger.exception("Failed to load version metadata for skill %s", skill_name)
                continue
            versions = meta.get("versions", {})
            if not isinstance(versions, dict):
                logger.warning("Skip invalid versions metadata for skill %s: %r", skill_name, versions)
                continue
            for version_name, version_meta in versions.items():
                if not isinstance(version_meta, dict):
                    logger.warning("Skip invalid version entry for skill %s version %s", skill_name, version_name)
                    continue
                if version_meta.get("status") != "candidate":
                    continue
                result.append(
                    {
                        "external_agent_id": f"skill:{skill_name}:{version_name}:{role}",
                        "agent_name": f"{skill_name}:{version_name}",
                        "client_type": "ws",
                        "client_url": ws_base,
                        "model_name": "werewolf-agent-langgraph",
                        "version": version_name,
                        "health": "available",
                        "status": "available",
                        "metadata": {
                            "mode": "skill_override",
                            "role_scope": role,
                            "skill_name": skill_name,
                            "skill_version": version_name,
                            "description": skill.get("description", ""),
                            "usage": version_meta.get("usage", {}),
                        },
                    }
                )
    return result



async def _process_init(agent_id: str, role: str, teammates: list,
                        req_id: str, external_agent_id: str | None = None) -> tuple[str, dict]:
    """初始化 agent: 铸造唯一 session_id, 创建初始状态并写入 SessionStore.

    返回 (session_id, response). session_id 作为该实例后续 perceive/act 的路由键.
    """
    session_id = uuid.uuid4().hex
    initial = make_initial_state(agent_id)
    initial["my_role"] = role
    initial["session_id"] = session_id
    initial["external_agent_id"] = external_agent_id or ""

    try:
        initial["versions_used"] = _build_versions_used(role, external_agent_id)
    except Exception:
        initial["versions_used"] = {}

    request = {"status": "start", "message": ",".join(teammates), "round": 0}
    state = await run_perceive(initial, request)
    store.create(session_id, state)
    logger.info(f"Agent {agent_id} initialized as {role}, session={session_id}")
    return session_id, {
        "type": "init_ok",
        "req_id": req_id,
        "session_id": session_id,
        "agent_id": agent_id,
        "external_agent_id": external_agent_id,
    }


async def _process_perceive(session_id: str, req_id: str, status: str,
                            message: str, round_num: int, traces: list) -> dict:
    """处理感知事件 (按 session_id 路由)."""
    state = store.get(session_id)
    if state is None:
        return {"type": "error", "req_id": req_id,
                "detail": "Session not found. Send init first."}

    request = {
        "status": status,
        "message": message,
        "round": round_num,
        "traces": traces or [],
    }
    new_state = await run_perceive(state, request)
    store.set(session_id, new_state)
    return {"type": "perceive_ok", "req_id": req_id}


async def _process_act(session_id: str, agent_id: str, req_id: str, status: str,
                       message: str, round_num: int) -> list:
    """处理行动请求 (按 session_id 路由).

    返回帧列表: reflection 非空时先下发一帧 thought (思考过程回传),
    再跟一帧 act_result.
    """
    state = store.get(session_id)
    if state is None:
        return [{"type": "error", "req_id": req_id,
                 "detail": "Session not found. Send init first."}]

    req_dict = {
        "status": status,
        "message": message,
        "round": round_num,
    }
    # LLM 调用是异步的，直接 await
    result = await run_act(state, req_dict)
    store.set(session_id, result)

    frames = []
    thought = (result.get("last_thought") or "").strip()
    logger.info(f"[DEBUG] last_thought from result: {thought[:100] if thought else '(empty)'}")

    # Filter out error messages from thoughts
    if thought and not thought.startswith("ERROR:"):
        frames.append({
            "type": "thought",
            "session_id": session_id,
            "agent_id": agent_id,
            "round": round_num,
            "phase": status,
            "content": thought,
            "seq": 0,
        })

    action = result.get("next_action", {})
    frames.append({
        "type": "act_result",
        "req_id": req_id,
        "result": action.get("result", "PASS"),
        "target": action.get("target"),
    })
    return frames


def _route_thread_id(msg: dict, fallback: str) -> str:
    """优先用 session_id 路由; 缺省回退到 agent_id (兼容老客户端)."""
    return msg.get("session_id") or msg.get("agent_id") or fallback


async def handle_connection(ws: ServerConnection):
    """处理 agent 的 WebSocket 连接 (一条连接可承载该 agent 的多个 session)."""
    agent_id = None
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send(json.dumps({"type": "error", "detail": "Invalid JSON"}))
                continue

            msg_type = msg.get("type")
            req_id = msg.get("req_id", "0")

            if msg_type == "init":
                agent_id = msg.get("agent_id", "")
                role = msg.get("role", "villager")
                teammates = msg.get("teammates", [])
                external_agent_id = msg.get("external_agent_id")
                _session_id, resp = await _process_init(
                    agent_id, role, teammates, req_id, external_agent_id
                )

            elif msg_type == "perceive":
                resp = await _process_perceive(
                    _route_thread_id(msg, agent_id or ""),
                    req_id,
                    msg.get("status", ""),
                    msg.get("message", ""),
                    msg.get("round", 1),
                    msg.get("traces", []),
                )

            elif msg_type == "act":
                resp = await _process_act(
                    _route_thread_id(msg, agent_id or ""),
                    agent_id or "",
                    req_id,
                    msg.get("status", ""),
                    msg.get("message", ""),
                    msg.get("round", 1),
                )

            elif msg_type == "game_over":
                resp = await _process_game_over(
                    _route_thread_id(msg, agent_id or ""),
                    req_id,
                    msg.get("result", "lost"),
                    msg.get("winner_role", ""),
                    msg.get("all_roles", {}),
                    msg.get("skip_evolution", False),
                    msg.get("room_id", ""),
                    msg.get("my_role", ""),
                )

            elif msg_type == "buffer_status":
                resp = await _process_buffer_status(
                    _route_thread_id(msg, agent_id or ""),
                    req_id,
                )

            elif msg_type == "rollback":
                resp = await _process_rollback(
                    req_id,
                    msg.get("skill_name", ""),
                    msg.get("target_version", ""),
                )

            elif msg_type == "force_confirm":
                resp = await _process_force_confirm(
                    _route_thread_id(msg, agent_id or ""),
                    req_id,
                    msg.get("cluster_id", ""),
                )

            else:
                resp = {"type": "error", "req_id": req_id,
                        "detail": f"Unknown message type: {msg_type}"}

            # 处理器可能返回单帧 (dict) 或多帧 (list, 如 thought + act_result).
            for frame in (resp if isinstance(resp, list) else [resp]):
                await ws.send(json.dumps(frame, ensure_ascii=False))

    except websockets.ConnectionClosed:
        logger.info(f"Agent {agent_id} disconnected")
    except Exception:
        logger.exception(f"Agent {agent_id} connection error")


def _http_response(status: http.HTTPStatus, body, content_type: str) -> Response:
    """Build a self-contained HTTP/1.1 response (used to short-circuit the WS handshake)."""
    if isinstance(body, str):
        body = body.encode("utf-8")
    headers = Headers([
        ("Date", email.utils.formatdate(usegmt=True)),
        ("Connection", "close"),
        ("Content-Length", str(len(body))),
        ("Content-Type", content_type),
    ])
    return Response(status.value, status.phrase, headers, body)


async def process_request(connection: ServerConnection, request: Request):
    """Serve the debug endpoints over plain HTTP on the same port as the WS server.

    Returning a Response short-circuits the WebSocket handshake; returning None
    lets the upgrade proceed normally so real agent clients still connect.

    Both endpoints accept an optional ?session_id=... filter so a single
    logical agent_id running multiple sessions can be inspected per session.
      GET /debug/prompts -> JSON prompt history
      GET /debug/view    -> HTML prompt debugger
    """
    parts = urlsplit(request.path)
    if parts.path not in ("/debug/prompts", "/debug/view"):
        return None

    session_id = parse_qs(parts.query).get("session_id", [None])[0]
    history = prompt_logger.get_history(session_id)

    if parts.path == "/debug/prompts":
        body = json.dumps(history, ensure_ascii=False)
        return _http_response(http.HTTPStatus.OK, body, "application/json; charset=utf-8")
    return _http_response(
        http.HTTPStatus.OK, render_prompts_html(history), "text/html; charset=utf-8"
    )


# ── game_over 处理 ────────────────────────────────────────

# 全局进化批跑（去抖动：把 12 席并发坍缩为每局一次）
# 每局 12 个外部 agent 各自发 game_over。过去每席都内联跑全局聚类/确认/
# Curator，导致 12 倍冗余与缓冲池竞态（曾因并发确认撞 uk 唯一键而崩）。
# 改为：每席只反思 + 入池；聚类/确认/过期清理/Curator 由一个去抖动任务在
# 最后一席入池后统一跑一次。
_global_pass_lock = asyncio.Lock()
_global_pass_task = None
_GLOBAL_PASS_DEBOUNCE_S = 8.0


async def _schedule_global_evolution_pass(cfg) -> None:
    """安排一次全局进化批跑；新的入池会重置去抖动计时，把并发坍缩为一次。"""
    global _global_pass_task
    if _global_pass_task is not None and not _global_pass_task.done():
        _global_pass_task.cancel()
    _global_pass_task = asyncio.create_task(_debounced_global_pass(cfg))


async def _debounced_global_pass(cfg) -> None:
    try:
        await asyncio.sleep(_GLOBAL_PASS_DEBOUNCE_S)
    except asyncio.CancelledError:
        return
    async with _global_pass_lock:
        try:
            await asyncio.to_thread(_run_global_evolution_pass, cfg)
        except Exception:
            logger.exception("Global evolution pass failed")


def _run_global_evolution_pass(cfg) -> None:
    """池级聚类，每局只跑一次（线程内执行，不阻塞事件循环）。

    确认和过期清理已移至独立定时任务 _confirmation_expire_loop，不再绑定游戏结束。
    Curator 有自己的 _curator_monitor_loop。
    """
    from evolution.buffer_pool import BufferPool
    from evolution.clustering import SuggestionClusterer

    pool = BufferPool(cfg)
    clusterer = SuggestionClusterer(cfg, pool)
    clusterer.process_pending()
    logger.info("Global evolution pass (clustering only) complete")


_CONFIRMATION_EXPIRE_INTERVAL_S = 3600  # 1 hour


async def _confirmation_expire_loop(interval: float):
    """每小时检查一次：确认满足条件的 cluster + 清理过期建议。"""
    while True:
        await asyncio.sleep(interval)
        try:
            from evolution.config import load_config
            from evolution.buffer_pool import BufferPool
            from evolution.confirmation import ConfirmationJudge
            from evolution.version_manager import VersionManager
            cfg = load_config()
            if not cfg.enabled:
                continue
            pool = BufferPool(cfg)
            # 只在有 cluster 时才跑
            status = pool.get_status()
            if status["cluster_count"] == 0 and status["expired_count"] == 0:
                continue
            vm = VersionManager(cfg)
            judge = ConfirmationJudge(cfg, pool, vm)
            confirmed = judge.check_all_clusters()
            if confirmed:
                logger.info(f"Confirmation expire loop: {len(confirmed)} clusters confirmed")
            expired = pool.expire_old_suggestions()
            if expired:
                logger.info(f"Confirmation expire loop: {expired} suggestions expired")
        except Exception:
            logger.exception("Confirmation/expire loop check failed")


_CURATOR_MONITOR_INTERVAL_S = 12 * 3600  # 12 hours


async def _curator_monitor_loop(interval: float):
    """每 interval 秒检查一次策略库变动，有变动则触发 Curator。"""
    while True:
        await asyncio.sleep(interval)
        try:
            from evolution.config import load_config
            from evolution.curator import Curator
            cfg = load_config()
            if not cfg.enabled or not cfg.curator.enabled:
                continue
            curator = Curator(cfg)
            if curator.should_run(is_game_in_progress=False):
                summary = curator.run()
                logger.info(f"Curator monitor triggered: {summary}")
        except Exception:
            logger.exception("Curator monitor check failed")


async def _process_game_over(session_id: str, req_id: str,
                              result: str, winner_role: str,
                              all_roles: dict,
                              skip_evolution: bool = False,
                              room_id: str = "",
                              my_role: str = "") -> dict:
    """对局结束：触发反思 + 记忆更新 + 缓冲池操作。"""
    state = store.get(session_id)
    if state is None:
        # Session 可能因容器重启被清理，仍写最小归档避免数据丢失
        logger.warning(f"Session {session_id} not found for game_over (room={room_id}). "
                       f"Saving minimal archive, skipping reflection pipeline.")
        _save_minimal_archive(room_id or session_id, result, winner_role, all_roles, my_role)
        return {"type": "game_over_ack", "req_id": req_id, "session_not_found": True}

    if skip_evolution:
        logger.info(f"Session {session_id}: skip_evolution=True (builtin AI in game), skipping post-game pipeline.")
        return {"type": "game_over_ack", "req_id": req_id, "skip_evolution": True}

    state["room_id"] = room_id
    asyncio.create_task(_run_post_game_pipeline(
        state, result, winner_role, all_roles, session_id, req_id, room_id
    ))

    return {"type": "game_over_ack", "req_id": req_id}


def _extract_player_behavior(state: dict, player_id: str) -> str:
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


def _save_minimal_archive(room_id: str, result: str, winner_role: str, all_roles: dict, my_role: str = ""):
    """Session 丢失时保存最小对局归档，确保数据不丢。"""
    from evolution.db import get_session
    from evolution.models import EvolutionGameArchive

    # 优先使用后端传来的本席位角色；缺失时退回 all_roles 第一个
    if not my_role and all_roles:
        my_role = next(iter(all_roles.values()), "") or ""

    session = get_session()
    try:
        existing = session.query(EvolutionGameArchive).filter_by(game_id=room_id).first()
        if existing:
            # 已有归档则不重复写入
            session.commit()
            return
        session.add(EvolutionGameArchive(
            game_id=room_id,
            room_id=room_id,
            my_role=my_role,
            result=result,
            day_count=0,
            has_builtin_ai=False,
            payload_json={
                "scene_tags": {"result": result, "wolf_aggression": "unknown"},
                "note": "minimal archive — session lost before pipeline could run",
                "winner_role": winner_role,
                "all_roles": all_roles,
            },
        ))
        session.commit()
        logger.info(f"Minimal archive saved: room={room_id}, role={my_role}, result={result}")
    except Exception:
        session.rollback()
        logger.exception(f"Failed to save minimal archive for room={room_id}")
    finally:
        session.close()


async def _run_post_game_pipeline(state: dict, result: str,
                                   winner_role: str, all_roles: dict,
                                   session_id: str, req_id: str,
                                   room_id: str = ""):
    """对局结束后完整管道：反思 → 缓冲 → 记忆更新。"""
    try:
        from evolution.config import load_config
        from evolution.reflection_engine import ReflectionEngine, format_game_trace
        from evolution.buffer_pool import BufferPool
        from evolution.version_manager import VersionManager
        from memory.game_archive import save_game, record_strategy_gap
        from memory.self_model import update_self_model
        from memory.opponent_model import update_opponent_from_game
        from agents.llm_caller import llm

        cfg = load_config()
        if not cfg.enabled:
            return

        # 1. Format trace
        game_trace = format_game_trace(state.get("events", []), state.get("players", {}))

        # 2. Get in-game flags (accumulated during gameplay by _reflect_node)
        flags = list(state.get("in_game_flags", []))

        # 3. Load current strategies
        vm = VersionManager(cfg)
        current_strategies = vm.format_skills_for_prompt(
            state["my_role"], state.get("phase", "")
        )

        # 3.1 Initialize buffer pool (shared for ingest + expire)
        pool = BufferPool(cfg)

        # 4. Execute reflection
        # 3.5 Format working memory
        working_memory_text = ""
        wm_data = state.get("working_memory")
        if wm_data:
            from memory.working_memory import WorkingMemory
            wm = WorkingMemory.from_dict(wm_data)
            working_memory_text = wm.format_for_prompt()

        engine = ReflectionEngine(cfg)
        reflection = engine.reflect(
            game_id=room_id or state.get("room_id", "unknown"),
            my_role=state["my_role"],
            my_seat=state["me_id"],
            result=result,
            game_trace=game_trace,
            in_game_flags=flags,
            current_strategies=current_strategies,
            working_memory_text=working_memory_text,
        )

        if reflection:
            # 5. Write to buffer pool
            pool.ingest(reflection)

            # 6. 聚类每局去抖动统一跑一次（坍缩 12 席并发）；确认/过期由独立定时任务处理
            await _schedule_global_evolution_pass(cfg)

            # 8. Record strategy_gap
            if reflection.suggestion.match_level in ("low", "strategy_gap"):
                record_strategy_gap(
                    reflection.game_id,
                    f"{reflection.scene_tags.role}_{reflection.scene_tags.critical_phase}"
                )

            # 9. Archive game
            import yaml
            from dataclasses import asdict
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

        # 10. Update self model (sync LLM → to_thread to avoid blocking event loop)
        await asyncio.to_thread(
            update_self_model,
            state["my_role"],
            result,
            state.get("last_thought", ""),
            llm,
        )

        # 10.1 Update opponent models (Layer 2) — concurrent to_thread to avoid blocking
        from memory.opponent_model import update_opponent_from_game
        my_seat = state.get("me_id", "")
        opponent_tasks = []
        for player_id, player_role in (all_roles or {}).items():
            if player_id == my_seat:
                continue
            behavior_summary = _extract_player_behavior(state, player_id)
            if behavior_summary:
                opponent_tasks.append(
                    asyncio.to_thread(
                        update_opponent_from_game,
                        player_id, player_role, behavior_summary, llm,
                    )
                )
        if opponent_tasks:
            await asyncio.gather(*opponent_tasks)

        # 10.5 Record version usage for version competition
        versions_used = state.get("versions_used", {})
        if versions_used:
            won = (result == "won")
            for skill_name, version in versions_used.items():
                vm.record_usage(skill_name, version, won)

        # 11/12. 确认 + 过期清理已移至独立定时任务 _confirmation_expire_loop（每小时一次）

        logger.info(f"Post-game pipeline complete for session {session_id}: result={result}")
    except Exception:
        logger.exception(f"Post-game pipeline failed for session {session_id}")


# ── HTTP 兼容端点 ──────────────────────────────────────────
#
# 与 WS 协议共享同一套处理逻辑 (_process_*), 让 HttpAgentClient / WisAgentClient
# 等纯 HTTP 客户端也能对接本服务。HTTP 与 WS 共享同一个 SessionStore。

async def _http_agent_info(request):
    """GET /agent/info — 探测端点, 返回模型名/版本/健康状态。"""
    return aiohttp_web.json_response({
        "model": "werewolf-agent-langgraph",
        "version": "2.1",
        "name": "Werewolf Agent (WS+HTTP)",
        "protocols": ["ws", "http"],
    })


async def _http_health(request):
    """GET /health — 健康检查。"""
    return aiohttp_web.json_response({"status": "ok"})


async def _http_provider_health(request):
    """GET /provider/health — orchestration-facing provider health."""
    return aiohttp_web.json_response({
        "status": "ok",
        "provider_name": "LangGraph Evolution Agent Service",
        "provider_type": "langgraph",
        "model_name": "werewolf-agent-langgraph",
        "version": "2.1",
        "capabilities": ["ws", "http", "versioned_agents", "evolution"],
    })


async def _http_provider_agents(request):
    """GET /provider/agents — orchestration-facing participant list."""
    return aiohttp_web.json_response(_list_provider_agents())


async def _http_agent_init(request):
    """POST /agent/init — 初始化 agent 会话。

    兼容两种请求格式:
    - HttpAgentClient: {"agent_id": "..."}
    - WisAgentClient:    {"agent_id": "...", "name": "...", "role": "..."}
    """
    data = await request.json()
    agent_id = data.get("agent_id", "")
    role = data.get("role", "villager")
    external_agent_id = data.get("external_agent_id")
    teammates_str = data.get("teammates", "")
    teammates = [t.strip() for t in teammates_str.split(",") if t.strip()] if teammates_str else []

    _sid, resp = await _process_init(agent_id, role, teammates, "http-0", external_agent_id)
    return aiohttp_web.json_response(resp)


async def _http_agent_perceive(request):
    """POST /agent/perceive — 感知事件 (fire-and-forget)。"""
    data = await request.json()
    session_id = data.get("session_id") or data.get("agent_id", "")
    resp = await _process_perceive(
        session_id,
        "http-0",
        data.get("status", ""),
        data.get("message", ""),
        data.get("round", 1),
        data.get("traces", []),
    )
    return aiohttp_web.json_response(resp)


async def _http_agent_act(request):
    """POST /agent/act — 行动请求, 同步返回决策结果。

    兼容两种请求格式:
    - HttpAgentClient: {"agent_id": "...", "status": "...", "message": "...", ...}
    - WisAgentClient:  {"name": "...", "status": "...", "message": "...", "role": "...", ...}
    """
    data = await request.json()
    session_id = data.get("session_id") or data.get("agent_id") or data.get("name", "")

    frames = await _process_act(
        session_id,
        data.get("agent_id") or data.get("name", ""),
        "http-0",
        data.get("status", ""),
        data.get("message", ""),
        data.get("round", 1),
    )

    # 从返回帧中提取 act_result (跳过 thought 帧)
    for frame in frames:
        if frame.get("type") == "act_result":
            return aiohttp_web.json_response(frame)
    # fallback: 如果没找到 act_result, 返回错误
    return aiohttp_web.json_response(
        {"type": "error", "detail": "No action result returned"}, status=500
    )


async def _http_agent_game_over(request):
    """POST /agent/game_over — 对局结束通知。"""
    data = await request.json()
    session_id = data.get("session_id") or data.get("agent_id", "")
    resp = await _process_game_over(
        session_id,
        "http-0",
        data.get("result", "lost"),
        data.get("winner_role", ""),
        data.get("all_roles", {}),
        data.get("skip_evolution", False),
        data.get("room_id", ""),
        data.get("my_role", ""),
    )
    return aiohttp_web.json_response(resp)


def _create_http_app():
    """创建 aiohttp HTTP 应用, 注册所有兼容端点。"""
    app = aiohttp_web.Application()
    app.router.add_get("/agent/info", _http_agent_info)
    app.router.add_get("/health", _http_health)
    app.router.add_get("/", _http_health)
    app.router.add_get("/provider/health", _http_provider_health)
    app.router.add_get("/provider/agents", _http_provider_agents)
    app.router.add_post("/agent/init", _http_agent_init)
    app.router.add_post("/agent/perceive", _http_agent_perceive)
    app.router.add_post("/agent/act", _http_agent_act)
    app.router.add_post("/agent/game_over", _http_agent_game_over)

    # Evolution dashboard API
    app.router.add_get("/evolution/overview", _evo_overview)
    app.router.add_get("/evolution/skills", _evo_skills)
    app.router.add_get("/evolution/skills/{skill_name}", _evo_skill_detail)
    app.router.add_post("/evolution/skills/{skill_name}/rollback", _evo_rollback)
    app.router.add_post("/evolution/skills/{skill_name}/pin", _evo_pin)
    app.router.add_get("/evolution/buffer", _evo_buffer)
    app.router.add_get("/evolution/buffer/clusters/{cluster_id}", _evo_cluster_detail)
    app.router.add_post("/evolution/buffer/clusters/{cluster_id}/force-confirm", _evo_force_confirm)
    app.router.add_get("/evolution/skills/{skill_name}/versions/{version}/content", _evo_version_content)
    app.router.add_delete("/evolution/skills/{skill_name}/versions/{version}", _evo_delete_version)
    app.router.add_post("/evolution/skills/{skill_name}/versions", _evo_create_version)
    app.router.add_get("/evolution/skills/{skill_name}/diff", _evo_diff)
    app.router.add_get("/evolution/gaps", _evo_gaps)
    app.router.add_get("/evolution/games", _evo_games)
    app.router.add_get("/evolution/curator/status", _evo_curator_status)
    app.router.add_post("/evolution/gepa/trigger", _evo_gepa_trigger)
    app.router.add_get("/evolution/gepa/status", _evo_gepa_status)
    app.router.add_post("/evolution/gepa/cancel", _evo_gepa_cancel)
    app.router.add_post("/evolution/summary/generate", _evo_summary_generate)
    app.router.add_get("/evolution/summary/latest", _evo_summary_latest)
    app.router.add_get("/evolution/logs", _evo_logs)

    return app


# ── buffer_status 处理 ────────────────────────────────────

async def _process_buffer_status(session_id: str, req_id: str) -> dict:
    """返回缓冲池状态。"""
    try:
        from evolution.config import load_config
        from evolution.buffer_pool import BufferPool

        cfg = load_config()
        pool = BufferPool(cfg)
        status = pool.get_status()

        return {"type": "buffer_status", "req_id": req_id, **status}
    except Exception as e:
        return {"type": "error", "req_id": req_id, "detail": str(e)}


# ── rollback 处理 ────────────────────────────────────────

async def _process_rollback(req_id: str, skill_name: str,
                             target_version: str) -> dict:
    """回退策略版本。"""
    try:
        from evolution.config import load_config
        from evolution.version_manager import VersionManager

        cfg = load_config()
        vm = VersionManager(cfg)
        success = vm.rollback(skill_name, target_version)

        return {
            "type": "rollback_result",
            "req_id": req_id,
            "success": success,
            "skill_name": skill_name,
            "target_version": target_version,
        }
    except Exception as e:
        return {"type": "error", "req_id": req_id, "detail": str(e)}


async def _process_force_confirm(session_id: str, req_id: str,
                                  cluster_id: str) -> dict:
    """人工强制确认某个 cluster，跳过防抖阈值检查。"""
    if not cluster_id:
        return {"type": "error", "req_id": req_id,
                "detail": "cluster_id is required"}
    try:
        from evolution.config import load_config
        from evolution.buffer_pool import BufferPool
        from evolution.confirmation import ConfirmationJudge
        from evolution.version_manager import VersionManager

        cfg = load_config()

        pool = BufferPool(cfg)
        cluster = pool.load_cluster(cluster_id)
        if not cluster:
            return {"type": "error", "req_id": req_id,
                    "detail": f"Cluster '{cluster_id}' not found"}

        vm = VersionManager(cfg)
        judge = ConfirmationJudge(cfg, pool, vm)

        target_skill = cluster.get("target_skill", "")
        suggestions = cluster.get("suggestions", [])
        if not target_skill or not suggestions:
            return {"type": "error", "req_id": req_id,
                    "detail": "Cluster has no target_skill or suggestions"}

        new_content = judge._synthesize_strategy(suggestions, target_skill)
        version_name = vm.create_new_version(
            skill_name=target_skill,
            content=new_content,
            source="manual_force_confirm",
            trigger_cluster=cluster_id,
        )
        pool.move_to_confirmed(cluster_id)

        return {
            "type": "force_confirm_result",
            "req_id": req_id,
            "success": True,
            "cluster_id": cluster_id,
            "skill_name": target_skill,
            "new_version": version_name,
        }
    except Exception as e:
        return {"type": "error", "req_id": req_id, "detail": str(e)}


# ── Evolution Dashboard HTTP API ─────────────────────────────

async def _evo_overview(request):
    try:
        from evolution.config import load_config
        from evolution.buffer_pool import BufferPool
        from evolution.db import get_session
        from evolution.models import EvolutionSkill, EvolutionRuntimeState, EvolutionGameArchive
        from sqlalchemy import func

        cfg = load_config()
        pool = BufferPool(cfg)
        status = pool.get_status()

        session = get_session()
        try:
            skill_count = session.query(func.count(EvolutionSkill.id)).scalar() or 0
            total_games = session.query(func.count(EvolutionGameArchive.id)).scalar() or 0
            curator_record = session.get(EvolutionRuntimeState, "curator")
            curator_last_run = (curator_record.payload_json or {}).get("last_run_at") if curator_record else None
        finally:
            session.close()

        return aiohttp_web.json_response({
            "pending_count": status["pending_count"],
            "cluster_count": status["cluster_count"],
            "confirmed_count": status["confirmed_count"],
            "expired_count": status["expired_count"],
            "skill_count": skill_count,
            "total_games": total_games,
            "curator_last_run": curator_last_run,
        })
    except Exception as e:
        return aiohttp_web.json_response({"detail": str(e)}, status=500)


async def _evo_skills(request):
    try:
        from evolution.config import load_config
        from evolution.skill_loader import SkillLoader
        cfg = load_config()
        loader = SkillLoader(cfg)
        return aiohttp_web.json_response(loader.list_skills())
    except Exception as e:
        return aiohttp_web.json_response({"detail": str(e)}, status=500)


async def _evo_skill_detail(request):
    try:
        from evolution.config import load_config
        from evolution.skill_loader import SkillLoader
        cfg = load_config()
        loader = SkillLoader(cfg)
        skill_name = request.match_info["skill_name"]
        detail = loader.get_skill_detail(skill_name)
        if not detail:
            return aiohttp_web.json_response({"detail": "Skill not found"}, status=404)
        return aiohttp_web.json_response(detail)
    except Exception as e:
        return aiohttp_web.json_response({"detail": str(e)}, status=500)


async def _evo_rollback(request):
    try:
        from evolution.config import load_config
        from evolution.version_manager import VersionManager
        cfg = load_config()
        vm = VersionManager(cfg)
        skill_name = request.match_info["skill_name"]
        target_version = request.query.get("target_version", "")
        success = vm.rollback(skill_name, target_version)
        if not success:
            return aiohttp_web.json_response({"detail": "Rollback failed"}, status=400)
        return aiohttp_web.json_response({"success": True, "skill_name": skill_name, "current_default": target_version})
    except Exception as e:
        return aiohttp_web.json_response({"detail": str(e)}, status=500)


async def _evo_pin(request):
    try:
        from evolution.config import load_config
        from evolution.version_manager import VersionManager
        cfg = load_config()
        vm = VersionManager(cfg)
        skill_name = request.match_info["skill_name"]
        version = request.query.get("version", "")
        pinned = request.query.get("pinned", "true").lower() == "true"
        success = vm.pin_version(skill_name, version, pinned)
        if not success:
            return aiohttp_web.json_response({"detail": "Pin failed"}, status=400)
        return aiohttp_web.json_response({"success": True, "skill_name": skill_name, "version": version, "pinned": pinned})
    except Exception as e:
        return aiohttp_web.json_response({"detail": str(e)}, status=500)


async def _evo_buffer(request):
    try:
        from evolution.config import load_config
        from evolution.buffer_pool import BufferPool
        cfg = load_config()
        pool = BufferPool(cfg)
        return aiohttp_web.json_response(pool.get_status())
    except Exception as e:
        return aiohttp_web.json_response({"detail": str(e)}, status=500)


async def _evo_cluster_detail(request):
    try:
        from evolution.config import load_config
        from evolution.buffer_pool import BufferPool
        cfg = load_config()
        pool = BufferPool(cfg)
        cluster_id = request.match_info["cluster_id"]
        detail = pool.load_cluster(cluster_id)
        if not detail:
            return aiohttp_web.json_response({"detail": "Cluster not found"}, status=404)
        return aiohttp_web.json_response(detail)
    except Exception as e:
        return aiohttp_web.json_response({"detail": str(e)}, status=500)


async def _evo_force_confirm(request):
    try:
        from evolution.config import load_config
        from evolution.buffer_pool import BufferPool
        from evolution.confirmation import ConfirmationJudge
        from evolution.version_manager import VersionManager

        def _do_confirm():
            cfg = load_config()
            pool = BufferPool(cfg)
            cluster_id = request.match_info["cluster_id"]
            cluster = pool.load_cluster(cluster_id)
            if not cluster:
                return None, "Cluster not found", 404
            target_skill = cluster.get("target_skill", "")
            suggestions = cluster.get("suggestions", [])
            if not target_skill or not suggestions:
                return None, "Cluster has no target_skill or suggestions", 400
            vm = VersionManager(cfg)
            judge = ConfirmationJudge(cfg, pool, vm)
            new_content = judge._synthesize_strategy(suggestions, target_skill)
            version_name = vm.create_new_version(target_skill, new_content, "manual_force_confirm", cluster_id)
            pool.move_to_confirmed(cluster_id)
            return {"success": True, "cluster_id": cluster_id, "skill_name": target_skill, "new_version": version_name}, None, 200

        result, err, status = await asyncio.to_thread(_do_confirm)
        if err:
            return aiohttp_web.json_response({"detail": err}, status=status)
        return aiohttp_web.json_response(result)
    except Exception as e:
        return aiohttp_web.json_response({"detail": str(e)}, status=500)


async def _evo_version_content(request):
    try:
        from evolution.config import load_config
        from evolution.skill_loader import SkillLoader
        cfg = load_config()
        loader = SkillLoader(cfg)
        skill_name = request.match_info["skill_name"]
        version = request.match_info["version"]
        content = loader.get_version_content(skill_name, version)
        if content is None:
            return aiohttp_web.json_response({"detail": "Not found"}, status=404)
        return aiohttp_web.json_response({"skill_name": skill_name, "version": version, "content": content})
    except Exception as e:
        return aiohttp_web.json_response({"detail": str(e)}, status=500)


async def _evo_delete_version(request):
    try:
        from evolution.config import load_config
        from evolution.version_manager import VersionManager
        cfg = load_config()
        vm = VersionManager(cfg)
        skill_name = request.match_info["skill_name"]
        version = request.match_info["version"]
        success = vm.delete_version(skill_name, version)
        if not success:
            return aiohttp_web.json_response({"detail": "Delete failed"}, status=400)
        return aiohttp_web.json_response({"success": True, "skill_name": skill_name, "deleted_version": version})
    except Exception as e:
        return aiohttp_web.json_response({"detail": str(e)}, status=500)


async def _evo_create_version(request):
    try:
        from evolution.config import load_config
        from evolution.version_manager import VersionManager
        cfg = load_config()
        vm = VersionManager(cfg)
        skill_name = request.match_info["skill_name"]
        body = await request.json()
        content = body.get("content", "")
        version_name = vm.create_new_version(skill_name, content)
        return aiohttp_web.json_response({"success": True, "skill_name": skill_name, "new_version": version_name})
    except Exception as e:
        return aiohttp_web.json_response({"detail": str(e)}, status=500)


async def _evo_diff(request):
    try:
        from evolution.config import load_config
        from evolution.skill_loader import SkillLoader
        cfg = load_config()
        loader = SkillLoader(cfg)
        skill_name = request.match_info["skill_name"]
        version_a = request.query.get("version_a", "")
        version_b = request.query.get("version_b", "")
        result = loader.diff_versions(skill_name, version_a, version_b)
        if not result:
            return aiohttp_web.json_response({"detail": "Not found"}, status=404)
        return aiohttp_web.json_response(result)
    except Exception as e:
        return aiohttp_web.json_response({"detail": str(e)}, status=500)


async def _evo_gaps(request):
    try:
        from memory.game_archive import get_frequent_gaps
        min_count = int(request.query.get("min_count", "3"))
        return aiohttp_web.json_response(get_frequent_gaps(min_count))
    except Exception as e:
        return aiohttp_web.json_response({"detail": str(e)}, status=500)


async def _evo_games(request):
    try:
        from evolution.db import get_session
        from evolution.models import EvolutionGameArchive
        from sqlalchemy import desc, text
        limit = int(request.query.get("limit", "20"))
        session = get_session()
        try:
            rows = session.query(EvolutionGameArchive).order_by(
                desc(EvolutionGameArchive.created_at)
            ).limit(limit).all()

            # 批量从 games 表补充元数据（players_count, round_count, duration_seconds, winner）
            game_ids = [row.game_id for row in rows]
            games_meta = {}
            if game_ids:
                try:
                    import json as _json
                    result = session.execute(
                        text("SELECT room_id, round_count, duration_seconds, players_json, events_json FROM games WHERE room_id IN :ids"),
                        {"ids": tuple(game_ids)},
                    )
                    for r in result:
                        meta = {
                            "round_count": r[1],
                            "duration_seconds": r[2],
                            "players_count": 0,
                            "players": [],
                            "winner": None,
                        }
                        try:
                            raw_players = r[3]
                            if isinstance(raw_players, str):
                                raw_players = _json.loads(raw_players)
                            players = raw_players or []
                            meta["players_count"] = len(players)
                            meta["players"] = [
                                {
                                    "id": p.get("id", ""),
                                    "name": (p.get("agent") or {}).get("name", p.get("label", "")),
                                    "role": p.get("role", ""),
                                    "agent_id": (p.get("agent") or {}).get("id"),
                                    "agent_name": (p.get("agent") or {}).get("name"),
                                    "is_alive": None,
                                }
                                for p in players
                            ]
                        except Exception:
                            pass
                        try:
                            raw_events = r[4]
                            if isinstance(raw_events, str):
                                raw_events = _json.loads(raw_events)
                            events = raw_events or {}
                            timeline = events.get("timeline", []) if isinstance(events, dict) else []
                            alive_map = {}
                            for evt in timeline:
                                if isinstance(evt, dict) and evt.get("type") == "game_over":
                                    meta["winner"] = (evt.get("state_after") or {}).get("winner")
                                    alive_info = ((evt.get("state_after") or {}).get("players") or {})
                                    for sid, sinfo in alive_info.items():
                                        alive_map[str(sid)] = sinfo.get("alive", None)
                                    break
                            if alive_map:
                                for p in meta["players"]:
                                    p["is_alive"] = alive_map.get(str(p["id"]), None)
                        except Exception:
                            pass
                        games_meta[r[0]] = meta
                except Exception:
                    pass

            result = []
            for row in rows:
                payload = row.payload_json or {}
                meta = games_meta.get(row.game_id, {})
                result.append({
                    "game_id": row.game_id,
                    "winner": meta.get("winner") or payload.get("winner"),
                    "round_count": meta.get("round_count") or payload.get("round_count") or row.day_count,
                    "players_count": meta.get("players_count") or payload.get("players_count", len(payload.get("players") or [])),
                    "duration_seconds": meta.get("duration_seconds") or payload.get("duration_seconds", 0),
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "players": meta.get("players") or payload.get("players") or [],
                    "has_builtin_ai": row.has_builtin_ai,
                })
            return aiohttp_web.json_response(result)
        finally:
            session.close()
    except Exception as e:
        return aiohttp_web.json_response({"detail": str(e)}, status=500)


async def _evo_curator_status(request):
    try:
        from evolution.config import load_config
        from evolution.db import get_session
        from evolution.models import EvolutionRuntimeState
        from datetime import datetime, timezone, timedelta

        cfg = load_config()

        session = get_session()
        try:
            record = session.get(EvolutionRuntimeState, "curator")
            payload = dict(record.payload_json) if record else {}
        finally:
            session.close()

        last_run_at = payload.get("last_run_at")
        last_game_end_at = payload.get("last_game_end_at")

        next_run_at = None
        if last_run_at:
            try:
                last_run_dt = datetime.fromisoformat(last_run_at)
                interval_delta = timedelta(hours=cfg.curator.interval_hours)
                candidate = last_run_dt + interval_delta
                if last_game_end_at:
                    last_game_dt = datetime.fromisoformat(last_game_end_at)
                    idle_delta = timedelta(hours=cfg.curator.min_idle_hours)
                    idle_candidate = last_game_dt + idle_delta
                    candidate = max(candidate, idle_candidate)
                next_run_at = candidate.isoformat()
            except (ValueError, TypeError):
                pass

        return aiohttp_web.json_response({
            "enabled": cfg.curator.enabled,
            "interval_hours": cfg.curator.interval_hours,
            "min_idle_hours": cfg.curator.min_idle_hours,
            "max_iterations": cfg.curator.max_iterations,
            "versioning": {
                "demotion_stale_days": cfg.versioning.demotion_stale_days,
                "demotion_archive_days": cfg.versioning.demotion_archive_days,
                "promotion_min_games": cfg.versioning.promotion_min_games,
                "promotion_min_win_rate_delta": cfg.versioning.promotion_min_win_rate_delta,
                "warmup_games": cfg.versioning.warmup_games,
                "max_versions_per_skill": cfg.versioning.max_versions_per_skill,
            },
            "clustering_model": cfg.clustering_model,
            "confirmation": {
                "normal_min_count": cfg.confirmation.normal_min_count,
                "normal_min_consistency_rate": cfg.confirmation.normal_min_consistency_rate,
                "normal_min_avg_causal_strength": cfg.confirmation.normal_min_avg_causal_strength,
                "fast_track_min_causal_strength": cfg.confirmation.fast_track_min_causal_strength,
                "fast_track_min_count": cfg.confirmation.fast_track_min_count,
            },
            "runtime": {
                "last_run_at": last_run_at,
                "last_game_end_at": last_game_end_at,
                "next_run_at": next_run_at,
            },
        })
    except Exception as e:
        return aiohttp_web.json_response({"detail": str(e)}, status=500)


# ── GEPA 离线进化 API ────────────────────────────────────

_gepa_task: asyncio.Task | None = None


async def _evo_gepa_trigger(request):
    global _gepa_task
    try:
        from evolution.gepa import get_status as gepa_get_status

        current = gepa_get_status()
        if current.get("status") == "running":
            return aiohttp_web.json_response(
                {"detail": "GEPA is already running"}, status=409)

        from evolution.config import load_config
        from evolution.gepa import GEPA

        cfg = load_config()

        if not cfg.gepa.enabled:
            return aiohttp_web.json_response(
                {"detail": "GEPA is disabled in config"}, status=400)

        gepa = GEPA(cfg)

        # Check prerequisites first
        prereq = gepa._check_prerequisites()
        if not prereq["ok"]:
            return aiohttp_web.json_response(
                {"detail": prereq["reason"]}, status=400)

        # Initialize state to running + launch background evolution
        trigger_result = gepa.trigger(cfg)
        if trigger_result.get("status") == "error":
            return aiohttp_web.json_response(
                {"detail": trigger_result.get("detail", "prerequisites failed")}, status=400)

        return aiohttp_web.json_response({"status": "started"})
    except Exception as e:
        logger.error(f"GEPA trigger error: {e}")
        return aiohttp_web.json_response({"detail": str(e)}, status=500)


async def _evo_gepa_status(request):
    try:
        from evolution.gepa import get_status as gepa_get_status
        status = gepa_get_status()
        return aiohttp_web.json_response(status)
    except Exception as e:
        return aiohttp_web.json_response({"detail": str(e)}, status=500)


async def _evo_gepa_cancel(request):
    global _gepa_task
    try:
        from evolution.gepa import cancel as gepa_cancel
        result = gepa_cancel()
        if _gepa_task and not _gepa_task.done():
            _gepa_task.cancel()
            _gepa_task = None
        return aiohttp_web.json_response(result)
    except Exception as e:
        return aiohttp_web.json_response({"detail": str(e)}, status=500)


# ── 自进化总结 API ────────────────────────────────────────

async def _evo_summary_generate(request):
    try:
        from evolution.summary import EvolutionSummary
        from evolution.config import load_config

        def _do_generate():
            cfg = load_config()
            summary = EvolutionSummary(cfg)
            return summary.generate(cfg)

        result = await asyncio.to_thread(_do_generate)
        return aiohttp_web.json_response(result)
    except Exception as e:
        return aiohttp_web.json_response({"detail": str(e)}, status=500)


async def _evo_summary_latest(request):
    try:
        from evolution.summary import EvolutionSummary
        from evolution.config import load_config

        cfg = load_config()
        summary = EvolutionSummary(cfg)
        result = summary.get_latest()
        if result is None:
            return aiohttp_web.json_response({"summary_text": None, "generated_at": None})
        return aiohttp_web.json_response(result)
    except Exception as e:
        return aiohttp_web.json_response({"detail": str(e)}, status=500)


async def _evo_logs(request):
    """查询自进化管道日志。支持 ?level=WARNING&logger=reflection&limit=100&hours=24"""
    try:
        from evolution.db import get_session
        from evolution.models import EvolutionPipelineLog
        from sqlalchemy import desc

        level = request.query.get("level", "")
        logger_name = request.query.get("logger", "")
        limit = min(int(request.query.get("limit", "100")), 500)
        hours = int(request.query.get("hours", "72"))

        session = get_session()
        try:
            from datetime import timedelta
            cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)
            q = session.query(EvolutionPipelineLog).filter(
                EvolutionPipelineLog.created_at >= cutoff
            )
            if level:
                q = q.filter(EvolutionPipelineLog.level == level)
            if logger_name:
                q = q.filter(EvolutionPipelineLog.logger_name.like(f"{logger_name}%"))
            q = q.order_by(desc(EvolutionPipelineLog.created_at)).limit(limit)

            logs = [
                {
                    "id": row.id,
                    "logger": row.logger_name,
                    "level": row.level,
                    "message": row.message,
                    "session_id": row.session_id,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in q.all()
            ]
            return aiohttp_web.json_response({"logs": logs, "count": len(logs)})
        finally:
            session.close()
    except Exception as e:
        return aiohttp_web.json_response({"detail": str(e)}, status=500)


async def _cleanup_loop(interval: float):
    """周期性清理过期 session, 释放内存."""
    while True:
        await asyncio.sleep(interval)
        removed = store.cleanup_expired()
        if removed:
            logger.info(f"Cleaned up {removed} expired session(s); {store.active_count()} active")


async def main(host: str = "0.0.0.0", port: int = 7861, http_port: int = 7860):
    """启动 WebSocket + HTTP Agent 服务.

    WS  服务在 ``port``  (默认 7861) — 供 WebSocketAgentClient 使用。
    HTTP 服务在 ``http_port`` (默认 7860) — 供 HttpAgentClient / WisAgentClient 使用。
    两者共享同一个 SessionStore, 处理逻辑完全一致。
    """
    stop = asyncio.get_event_loop().create_future()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_event_loop().add_signal_handler(sig, stop.set_result, None)
        except NotImplementedError:
            pass

    cleanup_interval = min(max(store.ttl / 4, 60.0), 300.0)
    cleanup_task = asyncio.create_task(_cleanup_loop(cleanup_interval))
    curator_monitor_task = asyncio.create_task(_curator_monitor_loop(_CURATOR_MONITOR_INTERVAL_S))
    confirmation_expire_task = asyncio.create_task(_confirmation_expire_loop(_CONFIRMATION_EXPIRE_INTERVAL_S))

    # ── HTTP 兼容服务 ──
    http_runner = None
    if HAS_AIOHTTP:
        http_app = _create_http_app()
        http_runner = aiohttp_web.AppRunner(http_app)
        await http_runner.setup()
        http_site = aiohttp_web.TCPSite(http_runner, host, http_port)
        await http_site.start()
        logger.info(f"HTTP compat service running on http://{host}:{http_port}")
    else:
        logger.warning("aiohttp not installed — HTTP compat endpoints disabled. "
                       "Install with: pip install aiohttp")

    # ── WebSocket 服务 ──
    async with websockets.serve(
        handle_connection, host, port, max_size=2**20, process_request=process_request
    ):
        logger.info(f"WS Agent Service running on ws://{host}:{port}")
        logger.info(f"Debug endpoints: http://{host}:{port}/debug/view (and /debug/prompts)")
        logger.info(f"Session TTL: {store.ttl}s, cleanup every {cleanup_interval}s")
        await stop

    # ── 清理 ──
    if http_runner:
        await http_runner.cleanup()
    cleanup_task.cancel()
    curator_monitor_task.cancel()
    confirmation_expire_task.cancel()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 7861
    http_port = int(sys.argv[2]) if len(sys.argv) > 2 else 7860
    asyncio.run(main(port=port, http_port=http_port))
