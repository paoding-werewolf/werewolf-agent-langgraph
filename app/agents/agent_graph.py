import re
from typing import Literal
from core.enums import Role, PHASE_CONFIG

from agents.state import AgentState
from agents.llm_caller import llm
from agents.prompt_builder import PromptBuilder


# ============================================================
# Perceive — processes incoming game events
# ============================================================

def _parse_event(state: AgentState) -> AgentState:
    """Process incoming ActRequest as a game event, updating state."""
    req = state.get("request")
    if not req:
        return state

    status = req.get("status", "")
    message = req.get("message", "")
    round_num = req.get("round", state["day"])

    event = {
        "status": status,
        "content": message,
        "round": round_num,
        "extra": req.get("extra") or {},
        "traces": req.get("traces") or [],
    }

    events = list(state["events"])
    events.append(event)

    players = {pid: dict(pdata) for pid, pdata in state["players"].items()}

    my_role = Role(state["my_role"])
    me_id = state["me_id"]

    # Phase-specific state updates
    if status == "start":
        if message and my_role.is_wolf_team:
            teammates = message.split(",")
            for tid in teammates:
                tid = tid.strip()
                if tid in players:
                    players[tid]["role"] = state["my_role"]

    elif status == "death_notice":
        ids = re.findall(r"\d+", message)
        for pid in ids:
            if pid in players:
                players[pid]["is_alive"] = False

    elif status == "sheriff":
        match = re.search(r"(\d+)号\s*当选", message)
        if match:
            sid = match.group(1)
            for p in players.values():
                p["is_sheriff"] = (p["id"] == sid)

    # Player metadata updates from traces
    for trace in event.get("traces", []):
        trace_from = str(trace.get("from", ""))
        trace_to = str(trace.get("to", ""))
        action = trace.get("action", "")
        if action == "wolf_kill" and trace_to in players:
            players[trace_to]["notes"] = "was attacked"
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
        "sheriff": _extract_sheriff_from_events(events, state["players"]),
    }


def _extract_sheriff_from_events(events, players):
    for e in reversed(events):
        if e.get("status") == "sheriff":
            match = re.search(r"(\d+)号\s*当选", e.get("content", ""))
            if match:
                return match.group(1)
        for pdata in players.values():
            if pdata.get("is_sheriff"):
                return pdata["id"]
    return None


# ============================================================
# Act — AI decision making with conditional branching
# ============================================================

def _reflect_node(state: AgentState) -> AgentState:
    """AI internal reflection: analyze the game state and form thoughts."""
    builder = PromptBuilder(Role(state["my_role"]), state["me_id"])

    task_guidance = """
[TASK: CRITICAL REFLECTION]
1. Scan the Game Progress Timeline. Identify logical contradictions.
2. Who is the most suspicious Wolf? Who are the confirmed Gods?
3. What is your current stance? Are you being suspected? How will you defend?
"""
    final_instr = "Output your internal monologue. Be concise and logical."

    gs = _to_agent_game_state(state)
    full_prompt = builder.build_decision_prompt(gs, task_guidance, final_instr, "")

    reflection = llm.call_with_log_sync(
        state["me_id"],
        f"{state['phase']}_reflect",
        "You are a Werewolf Logic Master. Focus on reasoning.",
        full_prompt,
        session_id=state.get("session_id", ""),
    )

    return {**state, "last_thought": reflection}


def _route_by_phase(state: AgentState) -> Literal[
    "decide_night_role", "decide_wolf_gesture", "decide_election",
    "decide_discussion", "decide_vote", "decide_shoot", "decide_generic",
]:
    """Conditional routing based on game phase group."""
    phase = state.get("phase", "")
    phase_cfg = PHASE_CONFIG.get(phase, {})
    group = phase_cfg.get("group", "")

    my_role = Role(state["my_role"])

    # Night: role-specific actions
    if group == "guard_action" and my_role == Role.GUARD:
        return "decide_night_role"
    if group == "wolf_kill" and my_role.is_wolf_team:
        return "decide_wolf_gesture"
    if group == "seer_check" and my_role == Role.SEER:
        return "decide_night_role"
    if group == "witch_action" and my_role == Role.WITCH:
        return "decide_night_role"

    # Election
    if group == "election":
        return "decide_election"

    # Daytime
    if group == "discussion":
        return "decide_discussion"
    if group == "vote":
        return "decide_vote"

    # Shooting
    if group in ("dawn_report", "shoot_skill") and my_role in (Role.HUNTER, Role.WOLF_KING):
        return "decide_shoot"

    return "decide_generic"


def _decide_night_role(state: AgentState) -> AgentState:
    """Night action for Guard/Seer/Witch."""
    builder = PromptBuilder(Role(state["my_role"]), state["me_id"])
    gs = _to_agent_game_state(state)
    req = state.get("request") or {}

    task_guidance = f"""
[TASK: NIGHT ACTION]
Current Phase: {req.get('status', state['phase'])}
Message: {req.get('message', '')}

Based on your internal monologue:
{state['last_thought']}

Choose the appropriate night action for your role.
"""
    final_instr = "Output your decision using the appropriate tool."

    full_prompt = builder.build_decision_prompt(gs, task_guidance, final_instr, state["last_thought"])

    action = llm.decide_with_tools_sync(
        state["me_id"], f"{state['phase']}_act",
        "You are a decisive Werewolf player. Use the tools provided.",
        full_prompt,
        session_id=state.get("session_id", ""),
    )

    return {**state, "next_action": action}


def _decide_wolf_gesture(state: AgentState) -> AgentState:
    """Wolf team communication at night."""
    builder = PromptBuilder(Role(state["my_role"]), state["me_id"])
    gs = _to_agent_game_state(state)
    req = state.get("request") or {}

    task_guidance = f"""
[TASK: WOLF TEAM ACTION]
Current Phase: {req.get('status', state['phase'])}
Message: {req.get('message', '')}

You are in the wolf night phase. Communicate with your wolf teammates.
Use wolf_gesture for communication or wolf_kill to choose a target.

Previous reflection:
{state['last_thought']}
"""
    final_instr = "Choose your action using the appropriate tool."

    full_prompt = builder.build_decision_prompt(gs, task_guidance, final_instr, state["last_thought"])

    action = llm.decide_with_tools_sync(
        state["me_id"], f"{state['phase']}_act",
        "You are a Werewolf player on the Wolf Team. Use tools to communicate and act.",
        full_prompt,
        session_id=state.get("session_id", ""),
    )

    return {**state, "next_action": action}


def _decide_election(state: AgentState) -> AgentState:
    """Sheriff election decisions."""
    builder = PromptBuilder(Role(state["my_role"]), state["me_id"])
    gs = _to_agent_game_state(state)
    req = state.get("request") or {}

    task_guidance = f"""
[TASK: SHERIFF ELECTION]
Current Phase: {req.get('status', state['phase'])}
Message: {req.get('message', '')}

Decide whether to sign up for sheriff or vote for a candidate.
Your previous reflection:
{state['last_thought']}
"""
    final_instr = "Use signup_sheriff, vote_sheriff, or pass_turn."

    full_prompt = builder.build_decision_prompt(gs, task_guidance, final_instr, state["last_thought"])

    action = llm.decide_with_tools_sync(
        state["me_id"], f"{state['phase']}_act",
        "You are a Werewolf player. Decide your election action using available tools.",
        full_prompt,
        session_id=state.get("session_id", ""),
    )

    return {**state, "next_action": action}


def _decide_discussion(state: AgentState) -> AgentState:
    """Daytime discussion: speak and analyze."""
    builder = PromptBuilder(Role(state["my_role"]), state["me_id"])
    gs = _to_agent_game_state(state)
    req = state.get("request") or {}

    task_guidance = f"""
[TASK: DAYTIME DISCUSSION]
Current Phase: {req.get('status', state['phase'])}
Message: {req.get('message', '')}

Your internal monologue:
{state['last_thought']}

Speak to the village. Share your analysis, point out suspects, defend yourself or allies.
Remember: be strategic, not emotional. Good wolves hide; good villagers find them.

PROTOCOL:
- Use speak tool for your speech
- If you have nothing useful to add, use pass_turn
"""
    final_instr = "Make your statement."

    full_prompt = builder.build_decision_prompt(gs, task_guidance, final_instr, state["last_thought"])

    action = llm.decide_with_tools_sync(
        state["me_id"], f"{state['phase']}_act",
        "You are a Werewolf player in daytime discussion. Use speak or pass_turn.",
        full_prompt,
        session_id=state.get("session_id", ""),
    )

    return {**state, "next_action": action}


def _decide_vote(state: AgentState) -> AgentState:
    """Elimination vote phase."""
    builder = PromptBuilder(Role(state["my_role"]), state["me_id"])
    gs = _to_agent_game_state(state)
    req = state.get("request") or {}

    task_guidance = f"""
[TASK: ELIMINATION VOTE]
Current Phase: {req.get('status', state['phase'])}
Message: {req.get('message', '')}

You must vote to eliminate a player (or pass/abstain).
Your analysis:
{state['last_thought']}

PROTOCOL:
- Use vote tool with target and reason
- Wolves: protect your teammates, bus if necessary
- Villagers: follow your most trusted player's lead
"""
    final_instr = "Cast your vote using the vote tool (or pass_turn to abstain)."

    full_prompt = builder.build_decision_prompt(gs, task_guidance, final_instr, state["last_thought"])

    action = llm.decide_with_tools_sync(
        state["me_id"], f"{state['phase']}_act",
        "You are a Werewolf player voting to eliminate. Use vote or pass_turn.",
        full_prompt,
        session_id=state.get("session_id", ""),
    )

    return {**state, "next_action": action}


def _decide_shoot(state: AgentState) -> AgentState:
    """Shoot skill for Hunter / Wolf King."""
    builder = PromptBuilder(Role(state["my_role"]), state["me_id"])
    gs = _to_agent_game_state(state)
    req = state.get("request") or {}

    task_guidance = f"""
[TASK: SHOOT SKILL]
Current Phase: {req.get('status', state['phase'])}
Message: {req.get('message', '')}

You are dying. Use your shoot skill to take someone with you (or pass).
{state['last_thought']}

PROTOCOL:
- Use shoot tool with target (or "pass" to not shoot)
- Hunter: take down a wolf if you know who
- Wolf King: take down a key good player
"""
    final_instr = "Use the shoot tool."

    full_prompt = builder.build_decision_prompt(gs, task_guidance, final_instr, state["last_thought"])

    action = llm.decide_with_tools_sync(
        state["me_id"], f"{state['phase']}_act",
        "You are using your shoot skill. Use shoot or pass_turn.",
        full_prompt,
        session_id=state.get("session_id", ""),
    )

    return {**state, "next_action": action}


def _decide_generic(state: AgentState) -> AgentState:
    """Fallback decision for phases without specific handling."""
    builder = PromptBuilder(Role(state["my_role"]), state["me_id"])
    gs = _to_agent_game_state(state)
    req = state.get("request") or {}

    task_guidance = f"""
[TASK: DECISION]
Current Phase: {req.get('status', state['phase'])}
Message: {req.get('message', '')}
{state['last_thought']}
"""
    final_instr = "Use pass_turn if you have nothing to do, or speak/generic action."

    full_prompt = builder.build_decision_prompt(gs, task_guidance, final_instr, state["last_thought"])

    action = llm.decide_with_tools_sync(
        state["me_id"], f"{state['phase']}_act",
        "You are a Werewolf player. Use available tools.",
        full_prompt,
        session_id=state.get("session_id", ""),
    )

    return {**state, "next_action": action}


# Phase-group -> decider. _route_by_phase returns one of these keys.
_DECIDERS = {
    "decide_night_role": _decide_night_role,
    "decide_wolf_gesture": _decide_wolf_gesture,
    "decide_election": _decide_election,
    "decide_discussion": _decide_discussion,
    "decide_vote": _decide_vote,
    "decide_shoot": _decide_shoot,
    "decide_generic": _decide_generic,
}


# ============================================================
# Helpers — bridge old dataclass types for PromptBuilder compat
# ============================================================

def _to_agent_game_state(state: AgentState):
    """Convert dict AgentState to AgentGameState dataclass for PromptBuilder."""
    from core.game_state import AgentGameState, PlayerPerception

    players = {}
    for pid, pdata in state["players"].items():
        role_val = pdata.get("role")
        players[pid] = PlayerPerception(
            id=pdata.get("id", pid),
            name=pdata.get("name", f"Player {pid}"),
            role=Role(role_val) if role_val else None,
            is_alive=pdata.get("is_alive", True),
            is_sheriff=pdata.get("is_sheriff", False),
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


# ============================================================
# Engine API — plain synchronous orchestration (no graph runtime)
# ============================================================

def run_perceive(state: AgentState, request: dict) -> AgentState:
    """Fold an incoming game event into the agent's state. No LLM call."""
    return _parse_event({**state, "request": request})


def run_act(state: AgentState, request: dict) -> AgentState:
    """Reflect, route by phase, then run the matching decider. Blocking (LLM)."""
    state = {**state, "request": request, "phase": request.get("status", state.get("phase", ""))}
    state = _reflect_node(state)
    decider = _DECIDERS[_route_by_phase(state)]
    return decider(state)