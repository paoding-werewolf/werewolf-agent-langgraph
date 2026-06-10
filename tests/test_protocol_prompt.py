import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/app")

from agents.agent_graph import _build_prompt_extra_data, _parse_event, _route_by_phase, _to_agent_game_state
from agents.llm_caller import LLMCaller
from agents.prompt_builder import PromptBuilder
from agents.protocol import normalize_action_status, normalize_event_status, normalize_status
from core.enums import Role
from core.game_state import AgentGameState, PlayerPerception
from memory.working_memory import WorkingMemory


def _state(phase="discussion", request=None):
    return {
        "room_id": "room",
        "me_id": "3",
        "my_role": "villager",
        "agent_id": "3_villager_test",
        "session_id": "s1",
        "phase": phase,
        "day": 1,
        "round": 1,
        "sheriff": "2",
        "players": {},
        "events": [],
        "last_thought": "",
        "next_action": None,
        "request": request,
        "working_memory": None,
        "strategies_used": [],
        "versions_used": {},
        "in_game_flags": [],
    }


def test_normalize_wire_status_for_event_and_action():
    assert normalize_status("discuss") == "discussion"
    assert normalize_status("sheriff_speech") == "sheriff_election_speech"
    assert normalize_status("sheriff", mode="event") == "sheriff_election_result"
    assert normalize_status("sheriff", mode="action") == "sheriff_transfer"
    assert normalize_status("death_notice") == "death_settlement"
    assert normalize_status("wolf_chat") == "wolf_chat"
    assert normalize_status("hunter", mode="event") == "shoot_begin"
    assert normalize_status("hunter", mode="action") == "shoot_skill"
    assert normalize_status("shoot_reminder") == "shoot_reminder"
    assert normalize_status("last_words") == "last_words"
    assert normalize_status("sheriff_transfer") == "sheriff_transfer"
    assert normalize_status("sheriff_pk_vote_result") == "sheriff_pk_vote_result"
    assert normalize_action_status("discuss", message="请发表遗言") == "last_words"
    assert normalize_action_status("last_words", message="请发表遗言") == "last_words"
    assert normalize_action_status("skill", message="请选择查验目标") == "seer_check"
    assert normalize_action_status("skill", previous_phase="guard_action") == "guard_action"
    assert normalize_event_status(
        "skill_result",
        [{"from": "5", "to": "2", "action": "seer_good"}],
    ) == "seer_check"
    assert normalize_event_status(
        "skill_result",
        [],
        message="预言家查验了 2号，结果为：好人",
    ) == "seer_check"
    assert normalize_event_status(
        "hunter",
        [{"from": "3", "to": "4", "action": "shoot_skill"}],
        message="3号 开枪带走了 4号",
    ) == "shoot_skill"
    assert normalize_event_status("hunter", [], message="3号 死亡，请发动技能") == "shoot_begin"
    assert normalize_event_status("shoot_reminder", [], message="你可以开枪") == "shoot_reminder"
    assert normalize_event_status("skill_result", [], message="你被毒杀，无法开枪") == "shoot_reminder"
    assert normalize_event_status(
        "sheriff_transfer",
        [{"from": "2", "to": "4", "action": "sheriff_transfer"}],
    ) == "sheriff_transfer"


def test_route_uses_current_action_status_not_previous_phase():
    state = _state(
        phase="dawn_report",
        request={"status": "discuss", "message": "请发言", "round": 1},
    )

    assert _route_by_phase(state) == "decide_discussion"


def test_route_handles_sheriff_speech_and_order_separately():
    speech_state = _state(
        phase="sheriff_election_signup",
        request={"status": "sheriff_speech", "message": "请发表竞选演讲", "round": 1},
    )
    order_state = _state(
        phase="sheriff_election_result",
        request={"status": "sheriff_speech_order", "message": "请选择发言方向", "round": 1},
    )

    assert _route_by_phase(speech_state) == "decide_discussion"
    assert _route_by_phase(order_state) == "decide_generic"


def test_route_handles_synced_server_phases():
    wolf_state = _state(
        phase="night_begin",
        request={"status": "wolf_chat", "message": "请与狼队友私聊沟通", "round": 1},
    )
    wolf_state["my_role"] = "wolf"
    election_state = _state(
        phase="sheriff_election_speech",
        request={"status": "sheriff_election_vote_begin", "message": "现在开始警长投票", "round": 1},
    )
    shoot_state = _state(
        phase="dawn_report",
        request={"status": "hunter", "message": "请发动技能", "round": 1},
    )
    shoot_state["my_role"] = "hunter"

    assert _route_by_phase(wolf_state) == "decide_wolf_gesture"
    assert _route_by_phase(election_state) == "decide_election"
    assert _route_by_phase(shoot_state) == "decide_shoot"


def test_route_handles_last_words_as_speech_action():
    state = _state(
        phase="shoot_skill",
        request={"status": "discuss", "message": "请发表遗言", "round": 1},
    )

    assert _route_by_phase(state) == "decide_discussion"


def test_route_handles_explicit_last_words_status():
    state = _state(
        phase="shoot_skill",
        request={"status": "last_words", "message": "请发表遗言", "round": 1},
    )

    assert _route_by_phase(state) == "decide_discussion"


def test_choose_speech_order_tool_returns_direction_for_server():
    action = LLMCaller()._tool_call_to_action("choose_speech_order", {"direction": "left"})

    assert action["result"] == "left"
    assert action["target"] is None


def test_route_restores_generic_skill_action_from_message():
    state = _state(
        phase="seer_check",
        request={"status": "skill", "message": "请选择查验目标", "round": 1},
    )
    state["my_role"] = "seer"

    assert _route_by_phase(state) == "decide_night_role"


def test_parse_event_normalizes_sheriff_result_and_updates_state():
    state = _state(request={"status": "sheriff", "message": "2号 当选警长", "round": 1})
    state["players"] = {
        str(i): {"id": str(i), "name": f"{i}号", "role": None}
        for i in range(1, 4)
    }

    new_state = _parse_event(state)

    assert new_state["phase"] == "sheriff_election_result"
    assert new_state["sheriff"] == "2"


def test_parse_event_infers_skill_result_phase_from_trace():
    state = _state(
        request={
            "status": "skill_result",
            "message": "预言家查验了 2号，结果为：好人",
            "round": 1,
            "traces": [{"from": "5", "to": "2", "action": "seer_good"}],
        }
    )
    state["players"] = {
        "2": {"id": "2", "name": "2号", "role": None},
        "5": {"id": "5", "name": "5号", "role": "seer"},
    }

    new_state = _parse_event(state)

    assert new_state["phase"] == "seer_check"
    assert new_state["events"][-1]["status"] == "seer_check"
    assert new_state["events"][-1]["wire_status"] == "skill_result"
    assert new_state["players"]["2"]["role"] == "villager"


def test_parse_event_marks_dead_players_after_death_notice_normalization():
    state = _state(request={
        "status": "death_notice",
        "message": "昨晚有玩家死亡",
        "round": 1,
        "traces": [{"from": None, "to": "2", "action": "death"}],
    })
    state["players"] = {
        "2": {"id": "2", "name": "2号", "role": None},
    }

    new_state = _parse_event(state)

    assert new_state["phase"] == "death_settlement"
    assert new_state["events"][-1]["status"] == "death_settlement"
    assert new_state["players"]["2"]["is_alive"] is False


def test_parse_event_marks_dead_players_after_dawn_report_with_poison():
    state = _state(request={
        "status": "night_info",
        "message": "天亮了，昨晚有玩家死亡",
        "round": 1,
        "traces": [
            {"from": None, "to": "2", "action": "death"},
            {"from": None, "to": "3", "action": "death"},
        ],
    })
    state["players"] = {
        str(i): {"id": str(i), "name": f"{i}号", "role": None, "is_alive": True}
        for i in range(1, 5)
    }

    new_state = _parse_event(state)
    game_state = _to_agent_game_state(new_state)
    prompt = PromptBuilder(Role.VILLAGER, "1").get_game_info(game_state)

    assert new_state["phase"] == "dawn_report"
    assert new_state["players"]["2"]["is_alive"] is False
    assert new_state["players"]["3"]["is_alive"] is False
    assert new_state["players"]["1"]["is_alive"] is True
    assert "👥 存活玩家: 1, 4" in prompt


def test_parse_event_marks_dead_player_after_final_vote_result_only():
    state = _state(request={
        "status": "vote_result",
        "message": "1号投给2号\n2号投给3号\n3号弃权",
        "round": 1,
        "traces": [
            {"from": "1", "to": "2", "action": "vote_eliminate"},
            {"from": "2", "to": "3", "action": "vote_eliminate"},
            {"from": "3", "to": None, "action": "vote_eliminate"},
        ],
    })
    state["players"] = {
        str(i): {"id": str(i), "name": f"{i}号", "role": None, "is_alive": True}
        for i in range(1, 5)
    }

    after_vote_details = _parse_event(state)

    assert all(player["is_alive"] for player in after_vote_details["players"].values())

    after_vote_details["request"] = {
        "status": "vote_result",
        "message": "2号 被投票出局",
        "round": 1,
        "traces": [{"from": None, "to": "2", "action": "vote_eliminate"}],
    }

    after_elimination = _parse_event(after_vote_details)
    game_state = _to_agent_game_state(after_elimination)
    prompt = PromptBuilder(Role.VILLAGER, "1").get_game_info(game_state)

    assert after_elimination["players"]["2"]["is_alive"] is False
    assert after_elimination["players"]["1"]["is_alive"] is True
    assert "👥 存活玩家: 1, 3, 4" in prompt


def test_parse_event_marks_shot_target_dead_and_updates_alive_prompt():
    state = _state(request={
        "status": "hunter",
        "message": "2号 开枪带走了 3号",
        "round": 1,
        "traces": [{"from": "2", "to": "3", "action": "shoot_skill"}],
    })
    state["players"] = {
        str(i): {"id": str(i), "name": f"{i}号", "role": None, "is_alive": True}
        for i in range(1, 5)
    }

    new_state = _parse_event(state)
    game_state = _to_agent_game_state(new_state)
    prompt = PromptBuilder(Role.VILLAGER, "1").get_game_info(game_state)

    assert new_state["phase"] == "shoot_skill"
    assert new_state["players"]["3"]["is_alive"] is False
    assert new_state["players"]["2"]["is_alive"] is True
    assert "👥 存活玩家: 1, 2, 4" in prompt


def test_parse_event_updates_sheriff_from_transfer_trace():
    state = _state(request={
        "status": "sheriff_transfer",
        "message": "将警徽转移给 4号",
        "round": 1,
        "traces": [{"from": "2", "to": "4", "action": "sheriff_transfer"}],
    })
    state["sheriff"] = "2"
    state["players"] = {
        str(i): {"id": str(i), "name": f"{i}号", "role": None, "is_alive": True}
        for i in range(1, 5)
    }

    new_state = _parse_event(state)
    game_state = _to_agent_game_state(new_state)

    assert new_state["phase"] == "sheriff_transfer"
    assert new_state["sheriff"] == "4"
    assert game_state.players["4"].is_sheriff is True
    assert game_state.players["2"].is_sheriff is False


def test_working_memory_keeps_multiple_speeches_in_same_day():
    wm = WorkingMemory(game_id="room", my_role="villager", my_seat="3", day=1)

    wm.update_from_event({
        "status": "discussion",
        "content": "1号：我是好人，先听后置位。",
        "round": 1,
        "traces": [{"from": "1", "to": None, "action": "speak"}],
    })
    wm.update_from_event({
        "status": "discussion",
        "content": "2号：警长发言，我会归票。",
        "round": 1,
        "traces": [{"from": "2", "to": None, "action": "speak"}],
    })

    prompt_text = wm.format_for_prompt()

    assert len(wm.speeches["D1"]) == 2
    assert "1号：我是好人，先听后置位。" in prompt_text
    assert "2号：警长发言，我会归票。" in prompt_text


def test_prompt_extra_data_injects_working_memory_object():
    state = _state(request={"status": "discuss", "message": "请发言", "round": 1})
    state["working_memory"] = {
        "game_id": "room",
        "my_role": "villager",
        "my_seat": "3",
        "day": 1,
        "known_info": [],
        "speeches": {
            "D1": {
                "发言#01 1号": "1号：我是好人，先听后置位。",
                "发言#02 2号": "2号：警长发言，我会归票。",
            }
        },
        "actions": [],
        "my_speeches": {},
        "contradictions": [],
        "flags": [],
        "suspicion": {"高": [], "中": [], "低": []},
    }

    extra_data = _build_prompt_extra_data(state)
    prompt = PromptBuilder(Role.VILLAGER, "3").build_decision_prompt(
        _to_agent_game_state(state),
        "任务",
        "最终指令",
        extra_data=extra_data,
        include_thinking_framework=False,
    )

    assert isinstance(extra_data["working_memory"], WorkingMemory)
    assert "### Speech Timeline" in prompt
    assert "1号：我是好人，先听后置位。" in prompt
    assert "2号：警长发言，我会归票。" in prompt


def test_parse_event_clears_sheriff_from_destroy_trace():
    state = _state(request={
        "status": "sheriff_transfer",
        "message": "撕毁了警徽",
        "round": 1,
        "traces": [{"from": "2", "to": None, "action": "sheriff_destroy"}],
    })
    state["sheriff"] = "2"
    state["players"] = {
        str(i): {"id": str(i), "name": f"{i}号", "role": None, "is_alive": True}
        for i in range(1, 5)
    }

    new_state = _parse_event(state)

    assert new_state["phase"] == "sheriff_transfer"
    assert new_state["sheriff"] is None


def test_current_discussion_prompt_includes_public_event_summary_without_future_timeline():
    game_state = AgentGameState(
        room_id="room",
        me_id="3",
        my_role=Role.VILLAGER,
        round=1,
        day=1,
        phase="discussion",
        sheriff="2",
        players={
            str(i): PlayerPerception(id=str(i), name=f"{i}号", is_alive=True)
            for i in range(1, 4)
        },
        events=[
            {
                "status": "discussion",
                "content": "1号：我是好人，先听后置位。",
                "round": 1,
                "traces": [{"from": "1", "to": None, "action": "speak"}],
            },
            {
                "status": "discussion",
                "content": "2号：警长发言，我会归票。",
                "round": 1,
                "traces": [{"from": "2", "to": None, "action": "speak"}],
            },
        ],
    )

    prompt = PromptBuilder(Role.VILLAGER, "3").get_game_info(game_state)

    assert "当前阶段: 白天发言" in prompt
    assert "1号：我是好人，先听后置位。" in prompt
    assert "2号：警长发言，我会归票。" in prompt
    assert "守卫" not in prompt
    assert "预言家已淘汰" not in prompt
