import logging
from typing import Dict, Any, Optional, List
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn

from agents.agent_graph import perceive_graph_compiled, act_graph_compiled
from agents.state import make_initial_state
from utils.prompt_logger import prompt_logger

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
async def get_prompts():
    return prompt_logger.get_history()


@app.get("/debug/view", response_class=HTMLResponse)
async def view_prompts():
    history = prompt_logger.get_history()

    cards = []
    for i, item in enumerate(reversed(history)):
        card = f"""
        <div class="card" style="border: 1px solid #ccc; margin-bottom: 10px; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 4px rgba(0,0,0,0.1);">
            <div class="card-header" onclick="toggle('card-{i}')" style="background: #f5f5f5; padding: 12px; cursor: pointer; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #eee;">
                <span>
                    <span style="color: #666; font-size: 0.9em;">[{item['timestamp']}]</span>
                    <strong style="margin-left: 10px;">Agent: {item['agent_id']}</strong>
                    <span style="background: #e3f2fd; color: #1976d2; padding: 2px 8px; border-radius: 4px; margin-left: 10px; font-size: 0.9em;">{item['phase']}</span>
                </span>
                <span id="icon-card-{i}">&#x25BC;</span>
            </div>
            <div id="card-{i}" class="card-body" style="display: none; padding: 15px; white-space: pre-wrap; font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace; background: #fff; font-size: 13px;">
                <div style="background: #fff3e0; padding: 10px; border-radius: 4px; border-left: 4px solid #ff9800; margin-bottom: 15px;">
                    <strong style="color: #e65100;">[SYSTEM PROMPT]</strong><br/>{item['system_prompt']}
                </div>
                <div style="background: #f1f8e9; padding: 10px; border-radius: 4px; border-left: 4px solid #4caf50; margin-bottom: 15px;">
                    <strong style="color: #1b5e20;">[USER MESSAGE]</strong><br/>{item['user_msg']}
                </div>
                <div style="background: #eceff1; padding: 10px; border-radius: 4px; border-left: 4px solid #607d8b;">
                    <strong style="color: #263238;">[LLM RESPONSE]</strong><br/>{item['response']}
                </div>
            </div>
        </div>
        """
        cards.append(card)

    html = f"""
    <!DOCTYPE html>
    <html>
        <head>
            <meta charset="UTF-8">
            <title>Agent Prompt Debugger</title>
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; padding: 30px; background: #fafafa; line-height: 1.5; }}
                .container {{ max-width: 1100px; margin: 0 auto; }}
                h1 {{ color: #24292e; border-bottom: 1px solid #eaecef; padding-bottom: 10px; }}
                .card:hover {{ box-shadow: 0 4px 8px rgba(0,0,0,0.15); }}
                hr {{ border: 0; border-top: 1px solid #eee; margin: 15px 0; }}
            </style>
            <script>
                function toggle(id) {{
                    var el = document.getElementById(id);
                    var icon = document.getElementById('icon-' + id);
                    if (el.style.display === 'none') {{
                        el.style.display = 'block';
                        icon.innerHTML = '&#x25B2;';
                    }} else {{
                        el.style.display = 'none';
                        icon.innerHTML = '&#x25BC;';
                    }}
                }}
            </script>
        </head>
        <body>
            <div class="container">
                <h1>Agent Prompt Debugger</h1>
                <p style="color: #666;"></p>
                <div id="list">
                    {''.join(cards) if cards else '<p style="text-align: center; color: #999; margin-top: 50px;"></p>'}
                </div>
            </div>
        </body>
    </html>
    """
    return html


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=7860)