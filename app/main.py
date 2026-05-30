import logging
from typing import Dict, Any, Optional, List
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn

from agents.agent_graph import perceive_graph_compiled, act_graph_compiled
from agents.state import make_initial_state
from utils.prompt_logger import prompt_logger
from utils.debug_view import render_prompts_html

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Werewolf LangGraph Agent Service")

# All state lives in LangGraph MemorySaver checkpointer now.
# agent_id = thread_id in config["configurable"]


class InitRequest(BaseModel):
    agent_id: str


class AgentInput(BaseModel):
    agent_id: str
    status: str
    message: Optional[str] = None
    round: int = 0
    extra: Optional[Dict[str, Any]] = None
    traces: Optional[List[Dict[str, Any]]] = None


def _config(agent_id: str) -> dict:
    return {"configurable": {"thread_id": agent_id}}


@app.post("/agent/checkHealth")
async def check_health():
    return {"status": "ok", "service": "werewolf-agent-langgraph"}


@app.post("/agent/init")
async def init_agent(req: InitRequest):
    logger.info(f"Initializing agent: {req.agent_id}")
    initial = make_initial_state(req.agent_id)
    initial["request"] = {}
    # HTTP transport routes by agent_id (thread_id == agent_id), so use it as
    # the session_id too — keeps debug entries consistent with the WS version.
    initial["session_id"] = req.agent_id
    # Seed the checkpointer with complete initial state
    result = await perceive_graph_compiled.ainvoke(initial, _config(req.agent_id))
    return {"status": "ok"}


@app.post("/agent/perceive")
async def perceive(inp: AgentInput):
    config = _config(inp.agent_id)

    # Check if this agent exists in the checkpointer
    existing = await perceive_graph_compiled.aget_state(config)
    if not existing or not existing.values:
        raise HTTPException(status_code=404, detail="Agent not found. Call /agent/init first.")

    request = inp.model_dump()
    base_state = existing.values
    input_state = {**base_state, "request": request}

    result = await perceive_graph_compiled.ainvoke(input_state, config)
    logger.info(f"Agent {inp.agent_id} state updated. Phase: {inp.status}")
    return {"status": "ok"}


@app.post("/agent/act")
async def act(inp: AgentInput):
    config = _config(inp.agent_id)

    existing = await perceive_graph_compiled.aget_state(config)
    if not existing or not existing.values:
        raise HTTPException(status_code=404, detail="Agent not found. Call /agent/init first.")

    base_state = existing.values
    # Merge request into state, using request status as current phase
    req_dict = inp.model_dump()
    input_state = {**base_state, "request": req_dict, "phase": req_dict["status"]}

    result = await act_graph_compiled.ainvoke(input_state, config)

    action = result.get("next_action", {})
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