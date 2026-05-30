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
import email.utils
import http
import json
import logging
import signal
import sys
import uuid
from urllib.parse import urlsplit, parse_qs

import websockets
from websockets.asyncio.server import ServerConnection
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

# 复用现有的 agent 核心逻辑
from agents.agent_graph import run_perceive, run_act
from agents.session_store import SessionStore
from agents.state import make_initial_state
from utils.prompt_logger import prompt_logger
from utils.debug_view import render_prompts_html

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ws_agent_service")


# 每个 session_id 一份独立状态; 空闲超过 TTL (默认 2h, 可配 SESSION_TTL_SECONDS) 后清理.
store = SessionStore()


async def _process_init(agent_id: str, role: str, teammates: list,
                        req_id: str) -> tuple[str, dict]:
    """初始化 agent: 铸造唯一 session_id, 创建初始状态并写入 SessionStore.

    返回 (session_id, response). session_id 作为该实例后续 perceive/act 的路由键.
    """
    session_id = uuid.uuid4().hex
    initial = make_initial_state(agent_id)
    initial["my_role"] = role
    initial["session_id"] = session_id

    request = {"status": "start", "message": ",".join(teammates), "round": 0}
    state = run_perceive(initial, request)
    store.create(session_id, state)
    logger.info(f"Agent {agent_id} initialized as {role}, session={session_id}")
    return session_id, {
        "type": "init_ok",
        "req_id": req_id,
        "session_id": session_id,
        "agent_id": agent_id,
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
    store.set(session_id, run_perceive(state, request))
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
    # LLM 调用是阻塞的, 放到线程池避免卡住事件循环.
    result = await asyncio.to_thread(run_act, state, req_dict)
    store.set(session_id, result)

    frames = []
    thought = (result.get("last_thought") or "").strip()
    if thought:
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
                _session_id, resp = await _process_init(
                    agent_id, role, teammates, req_id
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


async def _cleanup_loop(interval: float):
    """周期性清理过期 session, 释放内存."""
    while True:
        await asyncio.sleep(interval)
        removed = store.cleanup_expired()
        if removed:
            logger.info(f"Cleaned up {removed} expired session(s); {store.active_count()} active")


async def main(host: str = "0.0.0.0", port: int = 7861):
    """启动 WebSocket Agent 服务."""
    stop = asyncio.get_event_loop().create_future()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_event_loop().add_signal_handler(sig, stop.set_result, None)
        except NotImplementedError:
            pass

    cleanup_interval = min(max(store.ttl / 4, 60.0), 300.0)
    cleanup_task = asyncio.create_task(_cleanup_loop(cleanup_interval))

    async with websockets.serve(
        handle_connection, host, port, max_size=2**20, process_request=process_request
    ):
        logger.info(f"WS Agent Service running on ws://{host}:{port}")
        logger.info(f"Debug endpoints: http://{host}:{port}/debug/view (and /debug/prompts)")
        logger.info(f"Session TTL: {store.ttl}s, cleanup every {cleanup_interval}s")
        await stop

    cleanup_task.cancel()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 7861
    asyncio.run(main(port=port))