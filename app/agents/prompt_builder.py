import logging
from typing import Optional, Any, Dict

from core.enums import Role
from core.game_state import AgentGameState
from agents import prompt_storage

logger = logging.getLogger("werewolf_agent")

class PromptBuilder:
    """
    构建 Agent 决策提示词的类。
    从产品版本移植，保持高保真度，由共享状态机驱动。
    """

    ROLE_MAPPING = {
        "villager": Role.VILLAGER,
        "gentle_villager": Role.VILLAGER,
        "radical_villager": Role.VILLAGER,
        "meticulous_villager": Role.VILLAGER,
        "seer": Role.SEER,
        "witch": Role.WITCH,
        "wolf": Role.WOLF,
        "guard": Role.GUARD,
        "hunter": Role.HUNTER,
        "wolf_king": Role.WOLF_KING
    }

    PHASE_LABELS = {
        "init": "初始化",
        "start_game": "游戏开始",
        "night_begin": "夜晚开始",
        "guard_action": "守卫行动",
        "wolf_kill": "狼人行动",
        "seer_check": "预言家查验",
        "witch_action": "女巫行动",
        "death_settlement": "死亡结算",
        "dawn_report": "天亮播报",
        "sheriff_election_signup": "警长报名",
        "sheriff_election_speech": "警长竞选发言",
        "sheriff_election_vote": "警长投票",
        "sheriff_election_result": "警长结果公布",
        "sheriff_pk_speech": "警长 PK 发言",
        "sheriff_pk_vote_result": "警长 PK 投票结果",
        "sheriff_choose": "警长选择发言顺序",
        "discussion": "白天发言",
        "vote": "放逐投票",
        "shoot_skill": "开枪技能",
        "sheriff_transfer": "警徽移交",
        "last_words": "遗言",
        "game_over": "游戏结束",
    }

    PRIVATE_PHASES = {"guard_action", "wolf_kill", "seer_check", "witch_action"}

    def __init__(self, agent_role: Role, agent_id: str):
        self.agent_role = agent_role
        self.agent_id = agent_id

    def build_decision_prompt(self,
                              state: AgentGameState,
                              task_specific_guidance: str,
                              final_instruction: str,
                              last_thought: str = "",
                              extra_data: Optional[Dict[str, Any]] = None,
                              include_thinking_framework: bool = True) -> str:
        """
        主提示词构建器，从模块化部分组装提示词。
        """
        prompt = self._get_core_task(extra_data)
        prompt += self.get_game_info(state, extra_data)

        # ── Memory injection ──
        if extra_data and extra_data.get("working_memory"):
            prompt += "\n---\n" + extra_data["working_memory"].format_for_prompt()

        if extra_data and extra_data.get("opponent_profiles"):
            from memory.opponent_model import format_opponents_for_prompt
            prompt += "\n---\n" + format_opponents_for_prompt(extra_data["opponent_profiles"])

        if extra_data and extra_data.get("self_model_text"):
            prompt += "\n---\n" + extra_data["self_model_text"]

        # ── Evolved strategy injection ──
        if include_thinking_framework and not self.agent_role.is_wolf_team:
            strategy_text = ""
            if extra_data and extra_data.get("evolution_strategies"):
                strategy_text = extra_data["evolution_strategies"]

            if strategy_text:
                prompt += f"\n---\n## Active Strategies (Evolved)\n{strategy_text}\n---"

            prompt += f"""

---
This is a thinking framework for the villager role during daytime discussion, for reference only. It may not match your current role or day/night phase. Do not apply it rigidly:
``` Villager Perspective Thinking Framework
{prompt_storage.CRITICAL_THINKING_FRAMEWORK}
```
---"""

        # ── In-game flag prompt ──
        from evolution.in_game_flagger import IN_GAME_FLAG_PROMPT
        prompt += f"\n---\n{IN_GAME_FLAG_PROMPT}\n"

        if last_thought:
            prompt += f"\n### Your Previous Reflection\n{last_thought}\n"

        prompt += "\n---\n" + task_specific_guidance + "\n"
        prompt += "\n" + final_instruction + "\n"
        return prompt

    def _get_core_task(self, extra_data: Optional[Dict[str, Any]] = None) -> str:
        """第一部分：核心任务"""
        impersonate_role = (extra_data or {}).get("impersonate_role")
        viewpoint_role_str = impersonate_role if impersonate_role else self.agent_role.value

        if impersonate_role:
            camp = '好人阵营' if impersonate_role != 'wolf' else '狼人阵营'
        else:
            camp = '好人阵营' if not self.agent_role.is_wolf_team else '狼人阵营'

        return f"""
### 核心任务
你正在玩狼人杀游戏。你的角色是 【{viewpoint_role_str}】, 你的编号是 【{self.agent_id}】.
你的核心任务是：分析所有信息并为你所在的阵营 ({camp}).
"""

    def get_game_info(self, state: AgentGameState, extra_data: Optional[Dict[str, Any]] = None) -> str:
        """第二部分：游戏信息"""
        global_info = self._build_global_game_info(state, extra_data)
        progress_summary = self._build_current_phase_summary(state)
        public_events = self._build_public_event_summary(state)

        return f"""
### 游戏信息

**1. 全局游戏信息:**
{global_info}

**2. 当前阶段:**
{progress_summary}

**3. 已公开事件摘要:**
{public_events}
【重要提醒】: 只基于主持人已公开广播的事件判断当前局面，不要自行推演服务端内部状态机或未来阶段结果。
"""

    def _build_global_game_info(self, state: AgentGameState, extra_data: Optional[Dict[str, Any]] = None) -> str:
        """Builds the global game information section."""
        impersonate_role = (extra_data or {}).get("impersonate_role")
        if impersonate_role:
            viewpoint_role = self.ROLE_MAPPING.get(impersonate_role, self.agent_role)
        else:
            viewpoint_role = self.agent_role

        prompt = "--- 全局游戏信息开始 ---\n"
        prompt += f"🎭 你的角色: {viewpoint_role.value}\n"
        prompt += f"🏷️ 你的编号: {self.agent_id}\n"
        prompt += f"⏰ 当前回合: Day {state.day}\n"
        
        if state.sheriff:
            sheriff_display = self._add_you_marker(state.sheriff)
            prompt += f"🎖️ 当前警长: {sheriff_display}"
            if state.sheriff == self.agent_id:
                prompt += " ⭐ 你有+1票的优势 以及选择发言顺序的权利"
            prompt += "\n"
        else:
            prompt += "🎖️ Sheriff: 尚未选出\n"

        alive_ids = self._get_alive_player_ids(state)
        prompt += f"👥 存活玩家: {', '.join(alive_ids)}\n\n"

        if viewpoint_role.is_wolf_team:
            teammates = [p.id for p in state.players.values() if p.role and p.role.is_wolf_team]
            prompt += f"🐺 狼队友: {', '.join(teammates)}\n"

        if viewpoint_role == Role.SEER:
            verified = []
            for event in state.events:
                if event.get("status") == "seer_check":
                    for trace in event.get("traces", []):
                        if trace.get("from") == self.agent_id:
                            target = trace.get("to")
                            action = trace.get("action")
                            result = "Wolf" if action == "seer_wolf" else "Good"
                            verified.append(f"  └─ 查验 {target}: {result}")
            if verified:
                prompt += "🔮 已验证信息:\n" + "\n".join(verified) + "\n"

        prompt += "\n--- 全局游戏信息结束 ---\n\n"
        return prompt

    def _build_current_phase_summary(self, state: AgentGameState) -> str:
        label = self.PHASE_LABELS.get(state.phase, state.phase or "未知阶段")
        phase_group = self._phase_group(state.phase)
        visibility = "角色私有阶段" if phase_group in self.PRIVATE_PHASES else "公开阶段"
        return (
            f"- 当前回合: Day {state.day}\n"
            f"- 当前阶段: {label}\n"
            f"- 阶段分组: {phase_group}\n"
            f"- 可见性: {visibility}"
        )

    def _build_public_event_summary(self, state: AgentGameState) -> str:
        recent_events = state.events[-8:]
        if not recent_events:
            return "- 暂无公开事件"

        lines = []
        for event in recent_events:
            content = str(event.get("content") or "").strip()
            if not content:
                continue
            round_num = event.get("round", state.day)
            status = event.get("status", "")
            label = self.PHASE_LABELS.get(status, status)
            lines.append(f"- Day {round_num} {label}: {content}")

        return "\n".join(lines) if lines else "- 暂无可展示的公开事件"

    def _get_alive_player_ids(self, state: AgentGameState) -> list[str]:
        return [player_id for player_id, player in state.players.items() if player.is_alive]

    def _phase_group(self, phase: str) -> str:
        if phase in {
            "sheriff_election_signup",
            "sheriff_election_speech",
            "sheriff_election_vote",
            "sheriff_election_result",
            "sheriff_pk_speech",
            "sheriff_pk_vote_result",
        }:
            return "election"
        if phase in {"discussion", "sheriff_choose", "last_words"}:
            return "discussion"
        return phase or "unknown"

    def _add_you_marker(self, player_id: str) -> str:
        if str(player_id) == str(self.agent_id) or str(player_id) == str(self.agent_id).split("_")[0]:
            return f"{player_id}(你)"
        return str(player_id)
