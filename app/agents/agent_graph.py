import json
import re
from typing import TypedDict, List, Dict, Any, Optional
from langgraph.graph import StateGraph, END
from core.game_state import AgentGameState, PlayerPerception
from core.enums import Role
from utils.tui_visualizer import render_game_board
from agents.llm_caller import LLMCaller
from utils.prompt_logger import prompt_logger
from agents.prompt_builder import PromptBuilder

llm = LLMCaller()

class AgentState(TypedDict):
    room_id: str
    me_id: str
    my_role: str
    game_state: AgentGameState
    memory: List[Dict]
    last_thought: str
    next_action: Optional[Dict]
    request: Optional[Dict] # Current ActRequest

async def perceive_node(state: AgentState):
    """处理感知逻辑（已在外部完成，此处为图入口占位）"""
    return state

async def reflect_node(state: AgentState):
    """AI 自我反思：分析局势，更新内心想法"""
    builder = PromptBuilder(Role(state['my_role']), state['me_id'])
    
    task_guidance = """
[TASK: CRITICAL REFLECTION]
1. Scan the Game Progress Timeline. Identify logical contradictions.
2. Who is the most suspicious Wolf? Who are the confirmed Gods?
3. What is your current stance? Are you being suspected? How will you defend?
"""
    final_instr = "Output your internal monologue. Be concise and logical."

    full_prompt = builder.build_decision_prompt(
        state['game_state'], 
        task_guidance,
        final_instr,
        "" 
    )
    
    # 使用统一调用入口
    reflection = await llm.call_with_log(
        state['me_id'], 
        f"{state['game_state'].phase}_reflect",
        "You are a Werewolf Logic Master. Focus on reasoning.",
        full_prompt
    )
    
    return {**state, "last_thought": reflection}

async def act_node(state: AgentState):
    """执行动作：生成最终决策"""
    builder = PromptBuilder(Role(state['my_role']), state['me_id'])
    req = state['request']
    
    task_guidance = f"""
[TASK: DECISION MAKING]
Current Phase: {req['status']}
Judge Message: {req['message']}

Based on your internal monologue:
{state['last_thought']}
"""
    final_instr = """
You MUST output a valid JSON object:
{
  "result": "Your public speech or reason",
  "target": "target_player_id",
  "extra": {}
}
"""

    full_prompt = builder.build_decision_prompt(
        state['game_state'], 
        task_guidance,
        final_instr,
        state['last_thought']
    )
    
    # 使用统一调用入口
    response_text = await llm.call_with_log(
        state['me_id'], 
        f"{state['game_state'].phase}_act",
        "You are a decisive Werewolf player. Output JSON only.",
        full_prompt
    )
    
    # 简单的 JSON 提取逻辑
    try:
        match = re.search(r'\{.*\}', response_text, re.DOTALL)
        if match:
            action = json.loads(match.group())
        else:
            action = {"result": response_text}
    except:
        action = {"result": response_text}
        
    return {**state, "next_action": action}

def create_agent_graph():
    workflow = StateGraph(AgentState)
    
    workflow.add_node("perceive", perceive_node)
    workflow.add_node("reflect", reflect_node)
    workflow.add_node("act", act_node)
    
    workflow.set_entry_point("perceive")
    workflow.add_edge("perceive", "reflect")
    workflow.add_edge("reflect", "act")
    workflow.add_edge("act", END)
    
    return workflow.compile()
