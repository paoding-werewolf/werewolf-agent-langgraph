"""
WebSocket Agent Service — 与 WebSocketAgentClient 配套的落地实现。

复用全部现有 HTTP 版的 agent logic (agent_graph / state / prompt_builder / llm_caller),
仅将 transport 层从 HTTP FastAPI 改为 WebSocket 长连接。

协议:
  客户端 → 服务端: {"type": "init|perceive|act", "req_id": "...", "agent_id": "...", ...}
  服务端 → 客户端: {"type": "act_result", "req_id": "...", "result": "...", "target": "..."}

每个 agent 维持一条独立 WS 连接, 使用 LangGraph MemorySaver 在内存持久化状态.
"""

import asyncio
import json
import logging
import signal
import sys
import websockets
from websockets.asyncio.server import ServerConnection

# 复用现有的 agent 核心逻辑
from agents.agent_graph import (
    perceive_graph_compiled,
    act_graph_compiled,
    checkpointer,
)
from agents.state import make_initial_state

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ws_agent_service")


def _config(agent_id: str) -> dict:
    return {"configurable": {"thread_id": agent_id}}


async def _process_init(agent_id: str, role: str, teammates: list) -> dict:
    """初始化 agent: 创建初始状态并写入 checkpointer."""
    initial = make_initial_state(agent_id)
    initial["my_role"] = role
    initial["request"] = {"status": "start", "message": ",".join(teammates), "round": 0}

    result = await perceive_graph_compiled.ainvoke(initial, _config(agent_id))
    logger.info(f"Agent {agent_id} initialized as {role}")
    return {"type": "init_ok", "agent_id": agent_id}


async def _process_perceive(agent_id: str, status: str, message: str,
                            round_num: int, traces: list) -> dict:
    """处理感知事件."""
    config = _config(agent_id)
    existing = await perceive_graph_compiled.aget_state(config)
    if not existing or not existing.values:
        return {"type": "error", "detail": "Agent not found. Send init first."}

    request = {
        "status": status,
        "message": message,
        "round": round_num,
        "traces": traces or [],
    }
    base_state = existing.values
    input_state = {**base_state, "request": request}

    await perceive_graph_compiled.ainvoke(input_state, config)
    return {"type": "perceive_ok"}


async def _process_act(agent_id: str, req_id: str, status: str,
                       message: str, round_num: int) -> dict:
    """处理行动请求, 返回 act_result."""
    config = _config(agent_id)
    existing = await perceive_graph_compiled.aget_state(config)
    if not existing or not existing.values:
        return {"type": "error", "detail": "Agent not found. Send init first."}

    req_dict = {
        "status": status,
        "message": message,
        "round": round_num,
    }
    base_state = existing.values
    input_state = {**base_state, "request": req_dict, "phase": status}

    result = await act_graph_compiled.ainvoke(input_state, config)

    action = result.get("next_action", {})
    return {
        "type": "act_result",
        "req_id": req_id,
        "result": action.get("result", "PASS"),
        "target": action.get("target"),
    }


async def handle_connection(ws: ServerConnection):
    """处理单个 agent 的 WebSocket 连接."""
    agent_id = None
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send(json.dumps({"type": "error", "detail": "Invalid JSON"}))
                continue

            msg_type = msg.get("type")

            if msg_type == "init":
                agent_id = msg.get("agent_id", "")
                role = msg.get("role", "villager")
                teammates = msg.get("teammates", [])
                resp = await _process_init(agent_id, role, teammates)

            elif msg_type == "perceive":
                agent_id = msg.get("agent_id", agent_id or "")
                resp = await _process_perceive(
                    agent_id,
                    msg.get("status", ""),
                    msg.get("message", ""),
                    msg.get("round", 1),
                    msg.get("traces", []),
                )

            elif msg_type == "act":
                agent_id = msg.get("agent_id", agent_id or "")
                resp = await _process_act(
                    agent_id,
                    msg.get("req_id", "0"),
                    msg.get("status", ""),
                    msg.get("message", ""),
                    msg.get("round", 1),
                )

            else:
                resp = {"type": "error", "detail": f"Unknown message type: {msg_type}"}

            await ws.send(json.dumps(resp, ensure_ascii=False))

    except websockets.ConnectionClosed:
        logger.info(f"Agent {agent_id} disconnected")
    except Exception:
        logger.exception(f"Agent {agent_id} connection error")


async def main(host: str = "0.0.0.0", port: int = 7861):
    """启动 WebSocket Agent 服务."""
    stop = asyncio.get_event_loop().create_future()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_event_loop().add_signal_handler(sig, stop.set_result, None)
        except NotImplementedError:
            pass

    async with websockets.serve(handle_connection, host, port, max_size=2**20):
        logger.info(f"WS Agent Service running on ws://{host}:{port}")
        await stop


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 7861
    asyncio.run(main(port=port))