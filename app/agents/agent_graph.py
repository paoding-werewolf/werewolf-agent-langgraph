import re
from typing import Literal
from core.enums import Role, PHASE_CONFIG, GamePhase as GP

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

# Reflection and decision are merged into a single LLM call: the decider prompt
# asks the model to reason briefly, then emit the action tool call in one reply.
# This shared block replaces the former standalone reflect step.
_THINK_FIRST = """
[STEP 1 - ANALYZE] Reason briefly before acting:
- Scan the Game Progress Timeline for logical contradictions.
- Who is the most suspicious Wolf? Who are the confirmed Gods?
- What is your current stance? Are you suspected? How will you defend?
"""


def _route_by_phase(state: AgentState) -> Literal[
    "decide_night_role", "decide_wolf_gesture",
    "decide_election_signup", "decide_election_speech", "decide_election_vote",
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

    # Election — route to phase-specific handler
    if group == "election":
        if phase == GP.SHERIFF_ELECTION_SIGNUP:
            return "decide_election_signup"
        if phase in (GP.SHERIFF_ELECTION_SPEECH, GP.SHERIFF_PK_SPEECH):
            return "decide_election_speech"
        if phase in (GP.SHERIFF_ELECTION_VOTE, GP.SHERIFF_PK_VOTE):
            return "decide_election_vote"
        return "decide_election_signup"

    # Daytime
    if group == "discussion":
        return "decide_discussion"
    if group == "vote":
        return "decide_vote"

    # Shooting
    if group in ("dawn_report", "shoot_skill") and my_role in (Role.HUNTER, Role.WOLF_KING):
        return "decide_shoot"

    return "decide_generic"


def _build_extra_data(state: AgentState, phase: str) -> dict:
    """组装传入 prompt_builder 的额外数据（记忆 + 进化策略）。"""
    from memory.self_model import format_self_model_for_prompt
    from memory.working_memory import WorkingMemory

    extra = {}

    wm_data = state.get("working_memory")
    if wm_data:
        wm = WorkingMemory.from_dict(wm_data)
    else:
        wm = WorkingMemory(
            game_id=state.get("room_id", ""),
            my_role=state["my_role"],
            my_seat=state["me_id"],
            day=state.get("day", 1),
        )
    extra["working_memory"] = wm

    extra["self_model_text"] = format_self_model_for_prompt()

    try:
        from evolution.config import load_config
        from evolution.version_manager import VersionManager
        cfg = load_config()
        vm = VersionManager(cfg)
        extra["evolution_strategies"] = vm.format_skills_for_prompt(
            state["my_role"], phase
        )
    except Exception:
        pass

    return extra


def _decide_night_role(state: AgentState) -> AgentState:
    """Night action for Guard/Seer/Witch."""
    builder = PromptBuilder(Role(state["my_role"]), state["me_id"])
    gs = _to_agent_game_state(state)
    req = state.get("request") or {}
    extra_data = _build_extra_data(state, "seer_check")

    task_guidance = f"""
[TASK: NIGHT ACTION]
Current Phase: {req.get('status', state['phase'])}
Message: {req.get('message', '')}
{_THINK_FIRST}
[STEP 2 - ACT] Choose the appropriate night action for your role.
"""
    final_instr = "Reason briefly, then execute your decision with the appropriate tool in the same reply."

    full_prompt = builder.build_decision_prompt(gs, task_guidance, final_instr, "", extra_data=extra_data)

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
    extra_data = _build_extra_data(state, "wolf_kill")

    phase = req.get('status', state['phase'])
    if phase == GP.WOLF_GESTURE:
        task_guidance = f"""
[TASK: WOLF TEAM PRIVATE CHAT]
Current Phase: {phase}
Message: {req.get('message', '')}

You are in the wolf night phase. Chat privately with your wolf teammates.
Use wolf_chat to send a message (discuss who to kill tonight, coordinate strategy, share suspicions).
{_THINK_FIRST}
PROTOCOL:
- Use wolf_chat to send your message to teammates
- Discuss strategy: who to target, how to avoid suspicion, which roles you suspect
- Do NOT use wolf_kill here — this is the communication phase, killing comes next
"""
        final_instr = "Reason briefly, then send your message using wolf_chat in the same reply."
        sys_msg = "You are a Werewolf chatting with teammates at night. Use wolf_chat to send a message."
    else:
        task_guidance = f"""
[TASK: WOLF KILL]
Current Phase: {phase}
Message: {req.get('message', '')}

Choose a player to kill tonight.
{_THINK_FIRST}
PROTOCOL:
- Use wolf_kill with the target player ID
- Use pass_turn to abstain
"""
        final_instr = "Reason briefly, then use wolf_kill to choose your target."
        sys_msg = "You are a Werewolf choosing a kill target. Use wolf_kill or pass_turn."

    full_prompt = builder.build_decision_prompt(gs, task_guidance, final_instr, "", extra_data=extra_data)

    action = llm.decide_with_tools_sync(
        state["me_id"], f"{state['phase']}_act",
        sys_msg,
        full_prompt,
        session_id=state.get("session_id", ""),
    )

    return {**state, "next_action": action}


def _decide_election_signup(state: AgentState) -> AgentState:
    """Sheriff election signup: decide whether to run for sheriff."""
    builder = PromptBuilder(Role(state["my_role"]), state["me_id"])
    gs = _to_agent_game_state(state)
    req = state.get("request") or {}
    extra_data = _build_extra_data(state, "election")

    task_guidance = f"""
[TASK: SHERIFF ELECTION - SIGNUP]
Current Phase: {req.get('status', state['phase'])}
Message: {req.get('message', '')}

Decide whether YOU want to run for sheriff (上警).
{_THINK_FIRST}
PROTOCOL:
- You MUST call decide_signup with your decision
- decision="参选" if you want to run for sheriff
- decision="不参选" if you do NOT want to run
- Do NOT use any other tool here
"""
    final_instr = "Reason briefly, then call decide_signup with decision=参选 or decision=不参选."

    full_prompt = builder.build_decision_prompt(gs, task_guidance, final_instr, "", extra_data=extra_data)

    action = llm.decide_with_tools_sync(
        state["me_id"], f"{state['phase']}_act",
        "You MUST call decide_signup. decision=参选 to run, decision=不参选 to decline. No other tools.",
        full_prompt,
        session_id=state.get("session_id", ""),
    )

    return {**state, "next_action": action}


def _decide_election_speech(state: AgentState) -> AgentState:
    """Sheriff election / PK speech."""
    builder = PromptBuilder(Role(state["my_role"]), state["me_id"])
    gs = _to_agent_game_state(state)
    req = state.get("request") or {}
    extra_data = _build_extra_data(state, "election")

    task_guidance = f"""
[TASK: SHERIFF ELECTION - SPEECH]
Current Phase: {req.get('status', state['phase'])}
Message: {req.get('message', '')}

You are a sheriff candidate. Deliver your campaign speech to persuade voters.
{_THINK_FIRST}
PROTOCOL:
- Use speak tool for your campaign speech
- Be strategic: share analysis, build trust, convince others you should lead
"""
    final_instr = "Reason briefly, then deliver your speech with the speak tool in the same reply."

    full_prompt = builder.build_decision_prompt(gs, task_guidance, final_instr, "", extra_data=extra_data)

    action = llm.decide_with_tools_sync(
        state["me_id"], f"{state['phase']}_act",
        "You are a sheriff candidate giving a campaign speech. Use the speak tool.",
        full_prompt,
        session_id=state.get("session_id", ""),
    )

    return {**state, "next_action": action}


def _decide_election_vote(state: AgentState) -> AgentState:
    """Sheriff election / PK voting."""
    builder = PromptBuilder(Role(state["my_role"]), state["me_id"])
    gs = _to_agent_game_state(state)
    req = state.get("request") or {}
    extra_data = _build_extra_data(state, "election")

    task_guidance = f"""
[TASK: SHERIFF ELECTION - VOTE]
Current Phase: {req.get('status', state['phase'])}
Message: {req.get('message', '')}

Vote for ONE of the sheriff candidates listed above. The candidate IDs are in the Message.
{_THINK_FIRST}
PROTOCOL:
- Use vote_sheriff with target = candidate's numeric ID (e.g. "3")
- ONLY vote for a candidate listed in the Message — voting for non-candidates is wasted
- Use pass_turn to abstain
"""
    final_instr = "Reason briefly, then call vote_sheriff(target=\"<candidate_id>\") or pass_turn."

    full_prompt = builder.build_decision_prompt(gs, task_guidance, final_instr, "", extra_data=extra_data)

    action = llm.decide_with_tools_sync(
        state["me_id"], f"{state['phase']}_act",
        "You are voting for sheriff. Use vote_sheriff with a target player ID, or pass_turn to abstain.",
        full_prompt,
        session_id=state.get("session_id", ""),
    )

    return {**state, "next_action": action}


def _decide_discussion(state: AgentState) -> AgentState:
    """Daytime discussion: speak and analyze."""
    builder = PromptBuilder(Role(state["my_role"]), state["me_id"])
    gs = _to_agent_game_state(state)
    req = state.get("request") or {}
    extra_data = _build_extra_data(state, "discussion")

    task_guidance = f"""
[TASK: DAYTIME DISCUSSION]
Current Phase: {req.get('status', state['phase'])}
Message: {req.get('message', '')}
{_THINK_FIRST}
Speak to the village. Share your analysis, point out suspects, defend yourself or allies.
Remember: be strategic, not emotional. Good wolves hide; good villagers find them.

PROTOCOL:
- Use speak tool for your speech
- If you have nothing useful to add, use pass_turn
"""
    final_instr = "Reason briefly, then make your statement with the speak tool in the same reply."

    full_prompt = builder.build_decision_prompt(gs, task_guidance, final_instr, "", extra_data=extra_data)

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
    extra_data = _build_extra_data(state, "vote")

    task_guidance = f"""
[TASK: ELIMINATION VOTE]
Current Phase: {req.get('status', state['phase'])}
Message: {req.get('message', '')}

You must vote to eliminate a player (or pass/abstain).
{_THINK_FIRST}
PROTOCOL:
- Use vote tool with target and reason
- Wolves: protect your teammates, bus if necessary
- Villagers: follow your most trusted player's lead
"""
    final_instr = "Reason briefly, then cast your vote using the vote tool (or pass_turn to abstain) in the same reply."

    full_prompt = builder.build_decision_prompt(gs, task_guidance, final_instr, "", extra_data=extra_data)

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
    extra_data = _build_extra_data(state, "shoot_skill")

    task_guidance = f"""
[TASK: SHOOT SKILL]
Current Phase: {req.get('status', state['phase'])}
Message: {req.get('message', '')}

You are dying. Use your shoot skill to take someone with you (or pass).
{_THINK_FIRST}
PROTOCOL:
- Use shoot tool with target (or "pass" to not shoot)
- Hunter: take down a wolf if you know who
- Wolf King: take down a key good player
"""
    final_instr = "Reason briefly, then use the shoot tool in the same reply."

    full_prompt = builder.build_decision_prompt(gs, task_guidance, final_instr, "", extra_data=extra_data)

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
    extra_data = _build_extra_data(state, state.get("phase", ""))

    task_guidance = f"""
[TASK: DECISION]
Current Phase: {req.get('status', state['phase'])}
Message: {req.get('message', '')}
{_THINK_FIRST}
"""
    final_instr = "Reason briefly, then use pass_turn if you have nothing to do, or the appropriate tool, in the same reply."

    full_prompt = builder.build_decision_prompt(gs, task_guidance, final_instr, "", extra_data=extra_data)

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
    "decide_election_signup": _decide_election_signup,
    "decide_election_speech": _decide_election_speech,
    "decide_election_vote": _decide_election_vote,
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
    state = _parse_event({**state, "request": request})

    # Update working memory from the event
    try:
        from memory.working_memory import WorkingMemory
        wm_data = state.get("working_memory")
        wm = WorkingMemory.from_dict(wm_data) if wm_data else WorkingMemory(
            game_id=state.get("room_id", ""),
            my_role=state["my_role"],
            my_seat=state["me_id"],
            day=state.get("day", 1),
        )
        wm.update_from_event(request)
        wm.day = state.get("day", 1)
        state["working_memory"] = wm.to_dict()
    except Exception:
        pass

    return state


def run_act(state: AgentState, request: dict) -> AgentState:
    """Route by phase, then run the matching decider in a single LLM call. Blocking."""
    state = {**state, "request": request, "phase": request.get("status", state.get("phase", ""))}
    decider = _DECIDERS[_route_by_phase(state)]
    state = decider(state)
    thought = (state.get("next_action") or {}).get("thought", "")

    # Extract in-game flags from thought
    try:
        from evolution.in_game_flagger import InGameFlagger
        flagger = InGameFlagger()
        flags = flagger.extract_flags(thought)
        if flags:
            existing_flags = list(state.get("in_game_flags", []))
            existing_flags.extend(flags)
            state["in_game_flags"] = existing_flags

            # Also update working memory flags
            wm_data = state.get("working_memory")
            if wm_data:
                from memory.working_memory import WorkingMemory
                wm = WorkingMemory.from_dict(wm_data)
                for flag in flags:
                    wm.add_flag(flag)
                state["working_memory"] = wm.to_dict()
    except Exception:
        pass

    return {**state, "last_thought": thought}