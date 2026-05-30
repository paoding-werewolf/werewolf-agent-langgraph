import asyncio
import logging
from typing import Dict, Any, Optional, List
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn

from agents.agent_graph import run_perceive, run_act
from agents.session_store import SessionStore
from agents.state import make_initial_state
from utils.prompt_logger import prompt_logger
from utils.debug_view import render_prompts_html

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Per-agent state store. HTTP transport routes by agent_id (one logical agent
# per process), so agent_id doubles as the session key. Idle sessions expire
# after SESSION_TTL_SECONDS (default 2h).
store = SessionStore()


async def _cleanup_loop(interval: float):
    while True:
        await asyncio.sleep(interval)
        removed = store.cleanup_expired()
        if removed:
            logger.info(f"Cleaned up {removed} expired session(s); {store.active_count()} active")


@asynccontextmanager
async def lifespan(app: FastAPI):
    interval = min(max(store.ttl / 4, 60.0), 300.0)
    task = asyncio.create_task(_cleanup_loop(interval))
    logger.info(f"Session TTL: {store.ttl}s, cleanup every {interval}s")
    yield
    task.cancel()


app = FastAPI(title="Werewolf Agent Service", lifespan=lifespan)


class InitRequest(BaseModel):
    agent_id: str


class AgentInput(BaseModel):
    agent_id: str
    status: str
    message: Optional[str] = None
    round: int = 0
    extra: Optional[Dict[str, Any]] = None
    traces: Optional[List[Dict[str, Any]]] = None


@app.post("/agent/checkHealth")
async def check_health():
    return {"status": "ok", "service": "werewolf-agent"}


@app.post("/agent/init")
async def init_agent(req: InitRequest):
    logger.info(f"Initializing agent: {req.agent_id}")
    initial = make_initial_state(req.agent_id)
    # HTTP routes by agent_id, so use it as the session_id too — keeps debug
    # entries consistent with the WS version.
    initial["session_id"] = req.agent_id
    store.create(req.agent_id, initial)
    return {"status": "ok"}


@app.post("/agent/perceive")
async def perceive(inp: AgentInput):
    state = store.get(inp.agent_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Agent not found. Call /agent/init first.")

    store.set(inp.agent_id, run_perceive(state, inp.model_dump()))
    logger.info(f"Agent {inp.agent_id} state updated. Phase: {inp.status}")
    return {"status": "ok"}


@app.post("/agent/act")
async def act(inp: AgentInput):
    state = store.get(inp.agent_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Agent not found. Call /agent/init first.")

    # LLM call is blocking; offload to a thread so the event loop stays free.
    result = await asyncio.to_thread(run_act, state, inp.model_dump())
    store.set(inp.agent_id, result)

    action = result.get("next_action") or {}
    return {
        "success": True,
        "result": action.get("result", "PASS"),
        "target": action.get("target"),
        "extra": action.get("extra", {}),
    }


@app.get("/debug/prompts")
async def get_prompts(session_id: Optional[str] = None):
    return prompt_logger.get_history(session_id)


@app.get("/debug/view", response_class=HTMLResponse)
async def view_prompts(session_id: Optional[str] = None):
    return render_prompts_html(prompt_logger.get_history(session_id))


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=7860)