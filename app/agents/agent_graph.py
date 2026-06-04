import re
from typing import Literal
from core.enums import Role

from agents.state import AgentState
from agents.llm_caller import llm
from agents.prompt_builder import PromptBuilder
from agents.protocol import normalize_action_status, normalize_event_status

PRIVATE_NIGHT_ACTIONS = {
    "guard_action": Role.GUARD,
    "seer_check": Role.SEER,
    "witch_action": Role.WITCH,
}
WOLF_TEAM_PHASES = {"wolf_kill"}
WOLF_CHAT_PHASES = {"wolf_chat"}
DISCUSSION_PHASES = {"discussion", "sheriff_election_speech", "sheriff_pk_speech"}
ELECTION_PHASES = {
    "sheriff_election_signup",
    "sheriff_election_vote_begin",
    "sheriff_election_vote",
    "sheriff_election_result",
}
VOTE_PHASES = {"vote"}
SHOOT_PHASES = {"dawn_report", "shoot_skill"}

def _parse_event(state: AgentState) -> AgentState:
    """将传入的 ActRequest 作为游戏事件处理，更新状态。"""
    req = state.get("request")
    if not req:
        return state

    wire_status = req.get("status", "")
    message = req.get("message", "")
    round_num = req.get("round", state["day"])
    traces = req.get("traces") or []
    status = normalize_event_status(wire_status, traces, message=message)

    event = {
        "status": status,
        "wire_status": wire_status,
        "content": message,
        "round": round_num,
        "extra": req.get("extra") or {},
        "traces": traces,
    }

    events = list(state["events"])
    events.append(event)

    players = {pid: dict(pdata) for pid, pdata in state["players"].items()}
    state_sheriff = state.get("sheriff")

    my_role = Role(state["my_role"])

    # Phase-specific state updates
    if status == "start_game":
        if message and my_role.is_wolf_team:
            teammates = message.split(",")
            for tid in teammates:
                tid = tid.strip()
                if tid in players:
                    players[tid]["role"] = state["my_role"]

    elif status == "sheriff_election_result":
        match = re.search(r"(\d+)号\s*当选", message)
        if match:
            state_sheriff = match.group(1)

    # Player metadata updates from traces
    for trace in event.get("traces", []):
        trace_from = str(trace.get("from", ""))
        trace_to = str(trace.get("to", ""))
        action = trace.get("action", "")
        if action == "wolf_kill" and trace_to in players:
            players[trace_to]["notes"] = "遭到攻击"
        elif action == "seer_wolf" and trace_to in players:
            players[trace_to]["role"] = "wolf"
        elif action == "seer_good" and trace_to in players:
            players[trace_to]["role"] = "villager"

    return {
        **state,
        "phase": status,
        "day": round_num,
        "round": round_num,
        "players": players,
        "events": events,
        "sheriff": _extract_sheriff_from_events(events, state_sheriff),
    }


def _extract_sheriff_from_events(events, current_sheriff):
    for e in reversed(events):
        if e.get("status") == "sheriff_election_result":
            match = re.search(r"(\d+)号\s*当选", e.get("content", ""))
            if match:
                return match.group(1)
    return current_sheriff

async def _reflect_node(state: AgentState) -> AgentState:
    """AI 内部反思：分析游戏状态并形成思路。"""
    builder = PromptBuilder(Role(state["my_role"]), state["me_id"])

    task_guidance = """
[任务：关键反思]
1. 浏览当前公开信息与历史广播，找出逻辑矛盾。
2. 谁是最可疑的狼人？谁是已确认的神职？
3. 你目前的立场是什么？你是否被怀疑？你将如何辩护？
"""
    final_instr = "输出你的内心独白。请简洁且有逻辑。"

    gs = _to_agent_game_state(state)
    full_prompt = builder.build_decision_prompt(gs, task_guidance, final_instr, "")

    reflection = await llm.call_with_log(
        state["me_id"],
        f"{state['phase']}_reflect",
        "你是一名狼人杀逻辑大师。专注于推理。",
        full_prompt,
    )

    update = {"last_thought": reflection}

    # Extract in-game flags from this round's thought
    from evolution.in_game_flagger import InGameFlagger
    flagger = InGameFlagger()
    new_flags = flagger.extract_flags(reflection)
    if new_flags:
        existing_flags = state.get("in_game_flags", [])
        update["in_game_flags"] = existing_flags + new_flags

    return {**state, **update}


def _route_by_phase(state: AgentState) -> Literal[
    "decide_night_role", "decide_wolf_gesture", "decide_election",
    "decide_discussion", "decide_vote", "decide_shoot", "decide_generic",
]:
    """基于最小协议 phase 做条件路由。"""
    req = state.get("request") or {}
    phase = normalize_action_status(
        req.get("status") or state.get("phase", ""),
        message=req.get("message", ""),
        previous_phase=state.get("phase", ""),
    )

    my_role = Role(state["my_role"])

    if PRIVATE_NIGHT_ACTIONS.get(phase) == my_role:
        return "decide_night_role"
    if phase in WOLF_CHAT_PHASES and my_role.is_wolf_team:
        return "decide_wolf_gesture"
    if phase in WOLF_TEAM_PHASES and my_role.is_wolf_team:
        return "decide_wolf_gesture"
    if phase in DISCUSSION_PHASES:
        return "decide_discussion"
    if phase == "sheriff_choose":
        return "decide_generic"
    if phase in ELECTION_PHASES:
        return "decide_election"
    if phase in VOTE_PHASES:
        return "decide_vote"
    if phase in SHOOT_PHASES and my_role in (Role.HUNTER, Role.WOLF_KING):
        return "decide_shoot"

    return "decide_generic"


async def _decide_night_role(state: AgentState) -> AgentState:
    """守卫/预言家/女巫的夜间行动。"""
    builder = PromptBuilder(Role(state["my_role"]), state["me_id"])
    gs = _to_agent_game_state(state)
    req = state.get("request") or {}
    status = normalize_action_status(
        req.get("status", state["phase"]),
        message=req.get("message", ""),
        previous_phase=state.get("phase", ""),
    )

    task_guidance = f"""
[任务: 夜间行动]
当前阶段: {status}
消息: {req.get('message', '')}

基于你的内心独白：
{state['last_thought']}

选择适合你角色的夜间行动。
"""
    final_instr = "使用适当的工具输出你的决策。"

    full_prompt = builder.build_decision_prompt(gs, task_guidance, final_instr, state["last_thought"])

    action = await llm.decide_with_tools(
        state["me_id"], f"{state['phase']}_act",
        "你是一名果断的狼人杀玩家。使用提供的工具。",
        full_prompt,
    )

    return {**state, "next_action": action}


async def _decide_wolf_gesture(state: AgentState) -> AgentState:
    """狼人夜间交流。"""
    builder = PromptBuilder(Role(state["my_role"]), state["me_id"])
    gs = _to_agent_game_state(state)
    req = state.get("request") or {}
    status = normalize_action_status(
        req.get("status", state["phase"]),
        message=req.get("message", ""),
        previous_phase=state.get("phase", ""),
    )

    task_guidance = f"""
[任务：狼人团队行动]
当前阶段： {status}
消息： {req.get('message', '')}

你正处于狼人夜间阶段。与你的狼人队友交流。
使用 wolf_chat 进行交流或使用 wolf_kill 选择目标。

之前的反思：
{state['last_thought']}
"""
    final_instr = "使用适当的工具选择你的行动。"

    full_prompt = builder.build_decision_prompt(gs, task_guidance, final_instr, state["last_thought"])

    action = await llm.decide_with_tools(
        state["me_id"], f"{state['phase']}_act",
        "你是狼人阵营的玩家。使用工具进行交流并执行行动。",
        full_prompt,
    )

    return {**state, "next_action": action}


async def _decide_election(state: AgentState) -> AgentState:
    """警长选举决策。"""
    builder = PromptBuilder(Role(state["my_role"]), state["me_id"])
    gs = _to_agent_game_state(state)
    req = state.get("request") or {}
    status = normalize_action_status(
        req.get("status", state["phase"]),
        message=req.get("message", ""),
        previous_phase=state.get("phase", ""),
    )

    task_guidance = f"""
[任务：警长选举]
当前阶段： {status}
消息： {req.get('message', '')}

决定是否报名竞选警长或投票给候选人。
你之前的反思：
{state['last_thought']}
"""
    final_instr = "使用 signup_sheriff、vote_sheriff 或 pass_turn。"

    full_prompt = builder.build_decision_prompt(gs, task_guidance, final_instr, state["last_thought"])

    action = await llm.decide_with_tools(
        state["me_id"], f"{state['phase']}_act",
        "你是狼人杀玩家。使用可用工具决定你的竞选行动。",
        full_prompt,
    )

    return {**state, "next_action": action}


async def _decide_discussion(state: AgentState) -> AgentState:
    """白天讨论：发言和分析。"""
    builder = PromptBuilder(Role(state["my_role"]), state["me_id"])
    gs = _to_agent_game_state(state)
    req = state.get("request") or {}
    status = normalize_action_status(
        req.get("status", state["phase"]),
        message=req.get("message", ""),
        previous_phase=state.get("phase", ""),
    )

    task_guidance = f"""
[任务：白天讨论]
当前阶段： {status}
消息： {req.get('message', '')}

你的内心独白：
{state['last_thought']}

向全场玩家发言。分享你的分析、指出嫌疑人、为自己或盟友辩护。
记住：要策略性，不要情绪化。好的狼人隐藏身份；好的村民找出狼人。

行动规则：
- 使用 speak 工具进行发言
- 如果没有有用的内容可补充，使用 pass_turn
"""
    final_instr = "进行你的发言。"

    full_prompt = builder.build_decision_prompt(gs, task_guidance, final_instr, state["last_thought"])

    action = await llm.decide_with_tools(
        state["me_id"], f"{state['phase']}_act",
        "你是处于白天讨论阶段的狼人杀玩家。使用 speak 或 pass_turn。",
        full_prompt,
    )

    return {**state, "next_action": action}


async def _decide_vote(state: AgentState) -> AgentState:
    """放逐投票阶段。"""
    builder = PromptBuilder(Role(state["my_role"]), state["me_id"])
    gs = _to_agent_game_state(state)
    req = state.get("request") or {}
    status = normalize_action_status(
        req.get("status", state["phase"]),
        message=req.get("message", ""),
        previous_phase=state.get("phase", ""),
    )

    task_guidance = f"""
[任务：放逐投票]
当前阶段： {status}
消息： {req.get('message', '')}

你必须投票放逐一名玩家（或跳过/弃权）。
你的分析：
{state['last_thought']}

行动规则：
- 使用 vote 工具选择目标并说明理由
- 狼人：保护你的队友，必要时可出卖队友
- 村民：跟随你最信任的玩家
"""
    final_instr = "使用 vote 工具进行投票（或使用 pass_turn 弃权）。"

    full_prompt = builder.build_decision_prompt(gs, task_guidance, final_instr, state["last_thought"])

    action = await llm.decide_with_tools(
        state["me_id"], f"{state['phase']}_act",
        "你是正在进行放逐投票的狼人杀玩家。使用 vote 或 pass_turn。",
        full_prompt,
    )

    return {**state, "next_action": action}


async def _decide_shoot(state: AgentState) -> AgentState:
    """猎人/狼王的开枪技能。"""
    builder = PromptBuilder(Role(state["my_role"]), state["me_id"])
    gs = _to_agent_game_state(state)
    req = state.get("request") or {}
    status = normalize_action_status(
        req.get("status", state["phase"]),
        message=req.get("message", ""),
        previous_phase=state.get("phase", ""),
    )

    task_guidance = f"""
[任务：开枪技能]
当前阶段： {status}
消息： {req.get('message', '')}

你即将出局。使用开枪技能带走一名玩家（或跳过）。
{state['last_thought']}

行动规则：
- 使用 shoot 工具选择目标（或选择 "pass" 不开枪）
- 猎人：如果你知道谁是狼人，带走他
- 狼王：带走关键的好人玩家
"""
    final_instr = "使用 shoot 工具。"

    full_prompt = builder.build_decision_prompt(gs, task_guidance, final_instr, state["last_thought"])

    action = await llm.decide_with_tools(
        state["me_id"], f"{state['phase']}_act",
        "你正在使用开枪技能。使用 shoot 或 pass_turn。",
        full_prompt,
    )

    return {**state, "next_action": action}


async def _decide_generic(state: AgentState) -> AgentState:
    """没有特定处理阶段的回退决策。"""
    builder = PromptBuilder(Role(state["my_role"]), state["me_id"])
    gs = _to_agent_game_state(state)
    req = state.get("request") or {}
    status = normalize_action_status(
        req.get("status", state["phase"]),
        message=req.get("message", ""),
        previous_phase=state.get("phase", ""),
    )

    if status == "sheriff_choose":
        task_guidance = f"""
[任务：选择发言方向]
当前阶段： {status}
消息： {req.get('message', '')}

你是警长，需要选择白天发言从警左还是警右开始。
"""
        final_instr = "使用 choose_speech_order 工具返回 left 或 right。"
    else:
        task_guidance = f"""
[任务：决策]
当前阶段： {status}
消息： {req.get('message', '')}
{state['last_thought']}
"""
        final_instr = "如果没有要执行的操作，请使用 pass_turn，或者使用 speak/通用动作。"

    full_prompt = builder.build_decision_prompt(gs, task_guidance, final_instr, state["last_thought"])

    action = await llm.decide_with_tools(
        state["me_id"], f"{state['phase']}_act",
        "你是狼人杀玩家。使用可用工具。",
        full_prompt,
    )

    return {**state, "next_action": action}


def _to_agent_game_state(state: AgentState):
    """将字典类型的 AgentState 转换为 PromptBuilder 兼容的 AgentGameState 数据类。"""
    from core.game_state import AgentGameState, PlayerPerception

    dead_player_ids = set()
    for event in state.get("events", []):
        if event.get("status") != "death_settlement":
            continue
        dead_player_ids.update(re.findall(r"\d+", event.get("content", "")))

    players = {}
    for pid, pdata in state["players"].items():
        role_val = pdata.get("role")
        players[pid] = PlayerPerception(
            id=pdata.get("id", pid),
            name=pdata.get("name", f"玩家 {pid}"),
            role=Role(role_val) if role_val else None,
            notes=pdata.get("notes", ""),
        )

    return AgentGameState(
        room_id=state["room_id"],
        me_id=state["me_id"],
        my_role=Role(state["my_role"]),
        round=state.get("round", 1),
        day=state.get("day", 1),
        phase=state.get("phase", ""),
        players=players,
        events=list(state.get("events", [])),
        sheriff=state.get("sheriff"),
    )


async def perceive(state: AgentState, request: dict) -> AgentState:
    """处理一条来自服务端的公开事件。"""
    return _parse_event({**state, "request": request})


async def act(state: AgentState, request: dict) -> dict:
    """基于当前会话状态做出一次行动决策。"""
    input_state = {
        **state,
        "phase": normalize_action_status(
            request.get("status", state.get("phase", "")),
            message=request.get("message", ""),
            previous_phase=state.get("phase", ""),
        ),
        "request": {
            **request,
            "status": normalize_action_status(
                request.get("status", ""),
                message=request.get("message", ""),
                previous_phase=state.get("phase", ""),
            ),
        },
    }
    reflected_state = await _reflect_node(input_state)
    next_step = _route_by_phase(reflected_state)
    decision_handlers = {
        "decide_night_role": _decide_night_role,
        "decide_wolf_gesture": _decide_wolf_gesture,
        "decide_election": _decide_election,
        "decide_discussion": _decide_discussion,
        "decide_vote": _decide_vote,
        "decide_shoot": _decide_shoot,
        "decide_generic": _decide_generic,
    }
    return await decision_handlers[next_step](reflected_state)


# 向后兼容的别名导出
run_perceive = perceive
run_act = act
