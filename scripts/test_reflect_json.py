"""Test the new JSON reflect prompt against the real remote LLM."""
import asyncio
import sys
sys.path.insert(0, 'app')

from agents.prompt_builder import PromptBuilder
from agents.llm_caller import llm
from agents.agent_graph import _parse_reflection_json
from core.enums import Role
from core.game_state import AgentGameState, PlayerPerception


def build_minimal_state():
    """Build a minimal game state for testing."""
    players = {
        "1": PlayerPerception(id="1", role=Role.VILLAGER, is_alive=True),
        "2": PlayerPerception(id="2", role=Role.WOLF, is_alive=True),
        "3": PlayerPerception(id="3", role=Role.SEER, is_alive=True),
        "4": PlayerPerception(id="4", role=Role.VILLAGER, is_alive=True),
        "5": PlayerPerception(id="5", role=Role.WITCH, is_alive=True),
        "6": PlayerPerception(id="6", role=Role.VILLAGER, is_alive=True),
    }
    events = [
        {"status": "start_game", "round": 1, "content": "游戏开始"},
        {"status": "dawn_report", "round": 2, "content": "天亮了，昨晚3号玩家死亡。没有遗言。"},
        {"status": "discussion", "round": 2, "content": "4号发言：我觉得2号发言有问题，首夜信息少但2号的表态太刻意。5号发言：我同意，2号一直在带节奏。2号发言：我只是积极分析，你们这是找扛推。"},
    ]
    return AgentGameState(
        day=2,
        phase="discussion",
        players=players,
        events=events,
        sheriff=None,
    )


async def main():
    my_role = Role.VILLAGER
    my_id = "1"

    builder = PromptBuilder(my_role, my_id)
    state = build_minimal_state()

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

    full_prompt = builder.build_decision_prompt(
        state,
        task_guidance,
        final_instr,
        "",
        extra_data={},
        include_flag_prompt=False,
    )

    print("=" * 60)
    print("FULL PROMPT (first 2000 chars):")
    print("=" * 60)
    print(full_prompt[:2000])
    print("...")

    print("\n" + "=" * 60)
    print("CALLING LLM...")
    print("=" * 60)

    reflection = await llm.call_with_log(
        my_id,
        "discussion_reflect",
        "你是一名狼人杀逻辑大师。专注于推理。",
        full_prompt,
        "test_session",
        "test_agent",
    )

    print("\n" + "=" * 60)
    print("RAW LLM RESPONSE:")
    print("=" * 60)
    print(reflection)

    print("\n" + "=" * 60)
    print("PARSED RESULT:")
    print("=" * 60)
    parsed = _parse_reflection_json(reflection)
    print(f"  thought: {parsed['thought'][:200]}...")
    print(f"  flags ({len(parsed['flags'])}): {parsed['flags']}")
    print(f"  selected_strategies: {parsed['selected_strategies']}")

    # Validate
    assert parsed['thought'], "thought must not be empty"
    assert isinstance(parsed['flags'], list), "flags must be a list"
    assert isinstance(parsed['selected_strategies'], list), "selected_strategies must be a list"
    print("\n✅ All validations passed!")


if __name__ == '__main__':
    asyncio.run(main())
