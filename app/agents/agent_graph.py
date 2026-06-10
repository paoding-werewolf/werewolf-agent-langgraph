import json
import re
from typing import Literal
from core.enums import Role

from agents.state import AgentState
from agents.llm_caller import llm
from agents.prompt_builder import PromptBuilder
from agents.protocol import normalize_action_status, normalize_event_status

DEATH_TRACE_ACTIONS = {"death", "vote_eliminate", "shoot_skill"}


def _get_strategy_role(state: AgentState) -> str:
    my_role = state["my_role"]
    return "wolf" if my_role in {"wolf", "wolf_king"} else my_role


def _get_strategy_selection_index(state: AgentState) -> str:
    """为 reflect 阶段加载轻量策略索引，不注入全文。"""
    versions_used = state.get("versions_used", {})
    if not versions_used:
        return ""
    from evolution.config import load_config
    from evolution.version_manager import VersionManager
    cfg = load_config()
    vm = VersionManager(cfg)
    return vm.format_skill_selection_index(
        _get_strategy_role(state), versions_used
    )


def _get_selected_strategy_details(state: AgentState) -> str:
    """为 decision 阶段加载 reflect 选中的策略全文。"""
    versions_used = state.get("versions_used", {})
    selected = state.get("selected_strategies", [])
    if not versions_used or not selected:
        return ""
    from evolution.config import load_config
    from evolution.version_manager import VersionManager
    cfg = load_config()
    vm = VersionManager(cfg)
    return vm.format_selected_skills_for_prompt(
        _get_strategy_role(state), versions_used, selected
    )


def _build_prompt_extra_data(state: AgentState, strategy_mode: str = "details") -> dict:
    """组装 PromptBuilder 需要的可选上下文。"""
    if strategy_mode == "index":
        strategy_text = _get_strategy_selection_index(state)
    else:
        strategy_text = _get_selected_strategy_details(state)
    extra_data = {"evolution_strategies": strategy_text}
    personality_prompt = str(state.get("personality_prompt") or "").strip()
    if personality_prompt:
        extra_data["personality_prompt"] = personality_prompt
    wm_data = state.get("working_memory")
    if wm_data:
        from memory.working_memory import WorkingMemory
        extra_data["working_memory"] = WorkingMemory.from_dict(wm_data)
    return extra_data


def _parse_reflection_json(reflection: str) -> dict:
    """Parse reflection JSON output, handling markdown code-block wrapping.

    Returns {"thought": str, "flags": list[dict], "selected_strategies": list[str]}.
    Falls back to regex extraction if JSON parsing fails.
    """
    thought = reflection
    flags = []
    selected_strategies = []

    json_match = re.search(r"\{[\s\S]*\}", reflection)
    if json_match:
        try:
            data = json.loads(json_match.group(0))
            thought = data.get("thought", reflection)
            flags = data.get("flags", [])
            selected_strategies = data.get("selected_strategies", [])
            if isinstance(selected_strategies, list):
                selected_strategies = [s for s in selected_strategies if isinstance(s, str) and s.strip()]
            return {
                "thought": thought.strip() if isinstance(thought, str) else reflection,
                "flags": flags if isinstance(flags, list) else [],
                "selected_strategies": selected_strategies,
            }
        except (json.JSONDecodeError, TypeError):
            pass

    # Fallback: regex extraction for [SELECTED_STRATEGIES: ...]
    sel_match = re.search(r"\[SELECTED_STRATEGIES:\s*(.*?)\]", reflection, re.IGNORECASE | re.DOTALL)
    if sel_match:
        raw_items = re.split(r"[,，\n]+", sel_match.group(1))
        seen = set()
        for item in raw_items:
            key = item.strip().strip("`'\" ")
            if key and key.lower() not in {"none", "null", "无", "空"} and key not in seen:
                selected_strategies.append(key)
                seen.add(key)
            if len(selected_strategies) >= 3:
                break

    # Fallback: regex extraction for [FLAG] markers
    from evolution.in_game_flagger import InGameFlagger
    flagger = InGameFlagger()
    flags = flagger.extract_flags(reflection)

    return {
        "thought": thought.strip() if thought else reflection,
        "flags": flags,
        "selected_strategies": selected_strategies,
    }


PRIVATE_NIGHT_ACTIONS = {
    "guard_action": Role.GUARD,
    "seer_check": Role.SEER,
    "witch_action": Role.WITCH,
}
WOLF_TEAM_PHASES = {"wolf_kill"}
WOLF_CHAT_PHASES = {"wolf_chat"}
DISCUSSION_PHASES = {"discussion", "sheriff_election_speech", "sheriff_pk_speech", "last_words"}
ELECTION_PHASES = {
    "sheriff_election_signup",
    "sheriff_election_vote_begin",
    "sheriff_election_vote",
    "sheriff_election_result",
    "sheriff_pk_vote_result",
}
VOTE_PHASES = {"vote"}
SHOOT_PHASES = {"dawn_report", "shoot_skill"}


def _dead_player_ids_from_event(event: dict) -> set[str]:
    """根据已归一化事件 traces 推导本事件公开确认的死亡玩家。"""
    status = event.get("status", "")
    traces = event.get("traces") or []
    dead_ids: set[str] = set()

    if status in {"dawn_report", "death_settlement", "shoot_begin"}:
        for trace in traces:
            if trace.get("action") in DEATH_TRACE_ACTIONS and trace.get("to"):
                dead_ids.add(str(trace["to"]))

    elif status == "vote_result":
        for trace in traces:
            if trace.get("action") == "vote_eliminate" and trace.get("from") is None and trace.get("to"):
                dead_ids.add(str(trace["to"]))

    elif status == "shoot_skill":
        for trace in traces:
            if trace.get("action") == "shoot_skill" and trace.get("to"):
                dead_ids.add(str(trace["to"]))

    return dead_ids


def _update_working_memory(wm_data: dict | None, event: dict) -> dict | None:
    if not wm_data:
        return wm_data

    from memory.working_memory import WorkingMemory

    wm = WorkingMemory.from_dict(wm_data)
    wm.day = event.get("round", wm.day)
    wm.update_from_event(event)
    wm.compress_old_entries()
    return wm.to_dict()


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
    for pdata in players.values():
        pdata.setdefault("is_alive", True)
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
        elif action == "sheriff_transfer" and trace_to in players:
            state_sheriff = trace_to
        elif action == "sheriff_destroy":
            state_sheriff = None

    for dead_id in _dead_player_ids_from_event(event):
        if dead_id in players:
            players[dead_id]["is_alive"] = False

    return {
        **state,
        "phase": status,
        "day": round_num,
        "round": round_num,
        "players": players,
        "events": events,
        "sheriff": _extract_sheriff_from_events(events, state_sheriff),
        "working_memory": _update_working_memory(state.get("working_memory"), event),
    }


def _extract_sheriff_from_events(events, current_sheriff):
    for e in reversed(events):
        if e.get("status") == "sheriff_election_result":
            match = re.search(r"(\d+)号\s*当选", e.get("content", ""))
            if match:
                return match.group(1)
    return current_sheriff

async def _reflect_node(state: AgentState) -> AgentState:
    """AI 内部反思：分析游戏状态并形成思路。JSON 结构化输出。"""
    builder = PromptBuilder(Role(state["my_role"]), state["me_id"])

    task_guidance = """
[任务：关键反思]
1. 浏览当前公开信息与历史广播，找出逻辑矛盾。
2. 谁是最可疑的狼人？谁是已确认的神职？
3. 你目前的立场是什么？你是否被怀疑？你将如何辩护？
4. 审视当前生效的进化策略：策略前提是否成立？推荐行动是否合理？局势是否出现了策略未覆盖的情况？
5. 根据 Strategy Skill Index 选择当前最需要读取的策略 key。默认选择 0-1 条，明显相关时最多 3 条。
"""
    final_instr = (
        "以 JSON 格式输出（不要包含 ```json 代码块标记）。\n"
        "{\n"
        '  "thought": "你的内心独白和分析推理",\n'
        '  "flags": [\n'
        '    {"strategy_key": "策略key", "reason": "矛盾原因"}\n'
        "  ],\n"
        '  "selected_strategies": ["key1", "key2"]\n'
        "}\n"
        "如果没有发现策略矛盾，flags 为空数组 []。"
        "如果没有需要读取的策略，selected_strategies 为空数组 []。"
    )

    gs = _to_agent_game_state(state)
    full_prompt = builder.build_decision_prompt(
        gs,
        task_guidance,
        final_instr,
        "",
        extra_data=_build_prompt_extra_data(state, strategy_mode="index"),
        include_flag_prompt=False,
    )

    reflection = await llm.call_with_log(
        state["me_id"],
        f"{state['phase']}_reflect",
        "你是一名狼人杀逻辑大师。专注于推理。",
        full_prompt,
        state.get("session_id", ""),
        state.get("external_agent_id", ""),
    )

    parsed = _parse_reflection_json(reflection)

    # Build display text: thought + flags + selected strategies
    display_blocks = [parsed["thought"]]
    new_flags = parsed["flags"]
    selected = parsed["selected_strategies"]

    if new_flags:
        display_blocks.append("\n---\n### ⚑ 策略矛盾标记")
        for f in new_flags:
            if isinstance(f, dict):
                key = f.get("strategy_key", "?")
                reason = f.get("reason", str(f))
                display_blocks.append(f"- **{key}**: {reason}")

    if selected:
        display_blocks.append(f"\n---\n### 📋 选用策略: {', '.join(selected)}")

    update = {
        "last_thought": "\n".join(display_blocks),
        "selected_strategies": selected,
    }

    if new_flags:
        normalized = []
        for f in new_flags:
            if isinstance(f, dict):
                normalized.append({
                    "type": f.get("type", "strategy_mismatch"),
                    "description": f.get("reason", f.get("description", str(f))),
                    "detected_in": "thought",
                    "strategy_key": f.get("strategy_key", ""),
                })
        if normalized:
            existing_flags = state.get("in_game_flags", [])
            update["in_game_flags"] = existing_flags + normalized

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

    full_prompt = builder.build_decision_prompt(gs, task_guidance, final_instr, state["last_thought"], extra_data=_build_prompt_extra_data(state))

    action = await llm.decide_with_tools(
        state["me_id"], f"{state['phase']}_act",
        "你是一名果断的狼人杀玩家。使用提供的工具。",
        full_prompt,
        state.get("session_id", ""),
        state.get("external_agent_id", ""),
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

    full_prompt = builder.build_decision_prompt(gs, task_guidance, final_instr, state["last_thought"], extra_data=_build_prompt_extra_data(state))

    action = await llm.decide_with_tools(
        state["me_id"], f"{state['phase']}_act",
        "你是狼人阵营的玩家。使用工具进行交流并执行行动。",
        full_prompt,
        state.get("session_id", ""),
        state.get("external_agent_id", ""),
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

    full_prompt = builder.build_decision_prompt(gs, task_guidance, final_instr, state["last_thought"], extra_data=_build_prompt_extra_data(state))

    action = await llm.decide_with_tools(
        state["me_id"], f"{state['phase']}_act",
        "你是狼人杀玩家。使用可用工具决定你的竞选行动。",
        full_prompt,
        state.get("session_id", ""),
        state.get("external_agent_id", ""),
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

    full_prompt = builder.build_decision_prompt(gs, task_guidance, final_instr, state["last_thought"], extra_data=_build_prompt_extra_data(state))

    action = await llm.decide_with_tools(
        state["me_id"], f"{state['phase']}_act",
        "你是处于白天讨论阶段的狼人杀玩家。使用 speak 或 pass_turn。",
        full_prompt,
        state.get("session_id", ""),
        state.get("external_agent_id", ""),
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

    full_prompt = builder.build_decision_prompt(gs, task_guidance, final_instr, state["last_thought"], extra_data=_build_prompt_extra_data(state))

    action = await llm.decide_with_tools(
        state["me_id"], f"{state['phase']}_act",
        "你是正在进行放逐投票的狼人杀玩家。使用 vote 或 pass_turn。",
        full_prompt,
        state.get("session_id", ""),
        state.get("external_agent_id", ""),
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

    full_prompt = builder.build_decision_prompt(gs, task_guidance, final_instr, state["last_thought"], extra_data=_build_prompt_extra_data(state))

    action = await llm.decide_with_tools(
        state["me_id"], f"{state['phase']}_act",
        "你正在使用开枪技能。使用 shoot 或 pass_turn。",
        full_prompt,
        state.get("session_id", ""),
        state.get("external_agent_id", ""),
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

    full_prompt = builder.build_decision_prompt(gs, task_guidance, final_instr, state["last_thought"], extra_data=_build_prompt_extra_data(state))

    action = await llm.decide_with_tools(
        state["me_id"], f"{state['phase']}_act",
        "你是狼人杀玩家。使用可用工具。",
        full_prompt,
        state.get("session_id", ""),
        state.get("external_agent_id", ""),
    )

    return {**state, "next_action": action}


def _to_agent_game_state(state: AgentState):
    """将字典类型的 AgentState 转换为 PromptBuilder 兼容的 AgentGameState 数据类。"""
    from core.game_state import AgentGameState, PlayerPerception

    players = {}
    for pid, pdata in state["players"].items():
        role_val = pdata.get("role")
        players[pid] = PlayerPerception(
            id=pdata.get("id", pid),
            name=pdata.get("name", f"玩家 {pid}"),
            role=Role(role_val) if role_val else None,
            is_alive=pdata.get("is_alive", True),
            is_sheriff=(pid == state.get("sheriff")),
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
