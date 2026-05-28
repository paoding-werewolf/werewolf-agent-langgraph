import logging
from typing import Dict, Any, Optional, List
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn
import os

from core.game_state import AgentGameState, PlayerPerception
from core.enums import Role
from agents.agent_graph import create_agent_graph
from utils.prompt_logger import prompt_logger

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Werewolf LangGraph Agent Service")

# 全局存储 agent 状态
# agent_id -> AgentState
agents_registry: Dict[str, Dict[str, Any]] = {}
graph = create_agent_graph()

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
    return {"status": "ok", "service": "werewolf-agent-langgraph"}

@app.post("/agent/init")
async def init_agent(req: InitRequest):
    logger.info(f"Initializing agent: {req.agent_id}")
    # 解析 agent_id: {player_id}_{role}_{unique_id}
    parts = req.agent_id.split("_")
    player_id = parts[0]
    role_val = parts[1]
    
    # 初始化感知状态
    game_state = AgentGameState(
        room_id="unknown",
        me_id=player_id,
        my_role=Role(role_val)
    )
    
    # 初始填充 12 个玩家（暂定名，后面通过事件更新）
    for i in range(1, 13):
        pid = str(i)
        game_state.players[pid] = PlayerPerception(
            id=pid,
            name=f"Player {pid}",
            role=Role(role_val) if pid == player_id else None
        )

    agents_registry[req.agent_id] = {
        "room_id": "unknown",
        "me_id": player_id,
        "my_role": role_val,
        "game_state": game_state,
        "memory": [],
        "last_thought": "Game started. Waiting for information.",
        "next_action": None,
        "request": None
    }
    return {"status": "ok"}

@app.post("/agent/perceive")
async def perceive(inp: AgentInput):
    if inp.agent_id not in agents_registry:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    state = agents_registry[inp.agent_id]
    game_state: AgentGameState = state["game_state"]
    
    # 记录事件
    event = {
        "status": inp.status,
        "content": inp.message,
        "round": inp.round,
        "extra": inp.extra or {},
        "traces": inp.traces or []
    }
    game_state.events.append(event)
    game_state.phase = inp.status
    game_state.day = inp.round # 这里的 inp.round 实际上是 Day (1, 2, 3...)
    game_state.round = inp.round
    
    # 状态更新逻辑
    if inp.status == "start":
        # 狼人队友信息通常在 start 消息中：1,2,3
        if inp.message:
            teammates = inp.message.split(",")
            for tid in teammates:
                if tid in game_state.players:
                    game_state.players[tid].role = game_state.my_role

    elif inp.status == "death_notice":
        # 提取死者 ID (简单示例)
        import re
        ids = re.findall(r'\d+', inp.message)
        for pid in ids:
            if pid in game_state.players:
                game_state.players[pid].is_alive = False

    elif inp.status == "sheriff":
        # 警长产生
        import re
        match = re.search(r'(\d+)号 当选', inp.message)
        if match:
            sid = match.group(1)
            game_state.sheriff = sid
            for p in game_state.players.values():
                p.is_sheriff = (p.id == sid)

    logger.info(f"Agent {inp.agent_id} state updated. Phase: {inp.status}")
    return {"status": "ok"}

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
                <span id="icon-card-{i}">▼</span>
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
                        icon.innerText = '▲';
                    }} else {{
                        el.style.display = 'none';
                        icon.innerText = '▼';
                    }}
                }}
            </script>
        </head>
        <body>
            <div class="container">
                <h1>🕵️ Agent Prompt Debugger</h1>
                <p style="color: #666;">最近的请求排在最前面</p>
                <div id="list">
                    {''.join(cards) if cards else '<p style="text-align: center; color: #999; margin-top: 50px;">暂无日志</p>'}
                </div>
            </div>
        </body>
    </html>
    """
    return html

@app.post("/agent/act")
async def act(inp: AgentInput):
    if inp.agent_id not in agents_registry:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    state = agents_registry[inp.agent_id]
    state["request"] = inp.dict()
    
    # 运行 LangGraph 进行决策
    result = await graph.ainvoke(state)
    
    # 更新全局状态
    agents_registry[inp.agent_id] = result
    
    action = result.get("next_action", {})
    return {
        "success": True,
        "result": action.get("result", "PASS"),
        "target": action.get("target"),
        "extra": action.get("extra", {})
    }

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=7860)
