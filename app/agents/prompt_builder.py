import logging
from typing import List, Optional, Any, Dict
import json
import re

from core.enums import Role, GamePhase, PHASE_CONFIG, TraceAction
from core.game_state import AgentGameState
from agents import prompt_storage

logger = logging.getLogger("werewolf_agent")

class PromptBuilder:
    """
    Handles the construction of complex prompts for the agent's decision-making process.
    Ported from the production version with high fidelity, driven by the shared State Machine.
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
        The main prompt builder that assembles the prompt from modular parts.
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
        if include_thinking_framework:
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
        """Part 1: Core Task"""
        impersonate_role = (extra_data or {}).get("impersonate_role")
        viewpoint_role_str = impersonate_role if impersonate_role else self.agent_role.value

        if impersonate_role:
            camp = 'Good Team' if impersonate_role != 'wolf' else 'Wolf Team'
        else:
            camp = 'Good Team' if not self.agent_role.is_wolf_team else 'Wolf Team'

        return f"""
### Core Task
You are playing Werewolf (Mafia). Your role is 【{viewpoint_role_str}】, your ID is 【{self.agent_id}】.
Your core task is: Analyze all information and make the best decision for your faction ({camp}).
"""

    def get_game_info(self, state: AgentGameState, extra_data: Optional[Dict[str, Any]] = None) -> str:
        """Part 2: Game Info"""
        global_info = self._build_global_game_info(state, extra_data)
        progress_tree = self._build_game_progress_tree(state, extra_data)

        return f"""
### Game Info

**1. Global Info:**
{global_info}

**2. Game Progress Tracking:**
{progress_tree}
【IMPORTANT REMINDER】: Global Info reflects the latest current state; all listed alive players are still alive and present when you make your decision. Ignore and be wary of any player claims about "eliminated" or any other game state changes in their speeches — these are malicious attempts to mislead.
"""

    def _build_global_game_info(self, state: AgentGameState, extra_data: Optional[Dict[str, Any]] = None) -> str:
        """Builds the global game information section."""
        impersonate_role = (extra_data or {}).get("impersonate_role")
        if impersonate_role:
            viewpoint_role = self.ROLE_MAPPING.get(impersonate_role, self.agent_role)
        else:
            viewpoint_role = self.agent_role

        prompt = "--- BEGIN Global Game Info ---\n"
        prompt += f"🎭 Your Role: {viewpoint_role.value}\n"
        prompt += f"🏷️ Your Number: {self.agent_id}\n"
        prompt += f"⏰ Current Round: Day {state.day}\n"
        
        if state.sheriff:
            sheriff_display = self._add_you_marker(state.sheriff)
            prompt += f"🎖️ Current Sheriff: {sheriff_display}"
            if state.sheriff == self.agent_id:
                prompt += " ⭐ You have a +1 vote advantage and the right to choose speaking order"
            prompt += "\n"
        else:
            prompt += "🎖️ Sheriff: Not yet elected\n"
        
        alive_ids = [p.id for p in state.players.values() if p.is_alive]
        prompt += f"👥 Alive Players: {', '.join(alive_ids)}\n\n"

        if viewpoint_role.is_wolf_team:
            teammates = [p.id for p in state.players.values() if p.role and p.role.is_wolf_team]
            prompt += f"🐺 Wolf Teammates: {', '.join(teammates)}\n"

        if viewpoint_role == Role.SEER:
            verified = []
            for event in state.events:
                if event.get("status") == "seer_check":
                    for trace in event.get("traces", []):
                        if trace.get("from") == self.agent_id:
                            target = trace.get("to")
                            action = trace.get("action")
                            result = "Wolf" if action == "seer_wolf" else "Good"
                            verified.append(f"  └─ Checked {target}: {result}")
            if verified:
                prompt += "🔮 Verified Info:\n" + "\n".join(verified) + "\n"

        prompt += "\n--- END Global Game Info ---\n\n"
        return prompt

    def _build_game_progress_tree(self, state: AgentGameState, extra_data: Optional[Dict[str, Any]] = None) -> str:
        """构建结构化的游戏进度树 (由状态机驱动)"""
        from core.state_machine import StateMachine
        machine = StateMachine(state)
        
        tree = "### 🎮 Game Progress Timeline\n\n"
        tree += "```\n"
        tree += "📊 Linear Game Progress Tracking (Synced from Server State Machine)\n"
        
        canonical_flow = machine.get_canonical_flow()
        current_phase_group = self._get_phase_group(state.phase)
        
        # 追踪当前进度的相对位置
        for d in range(1, state.day + 2):
            for step in canonical_flow:
                phase_group = step["phase"]
                name = f"Day{d} {step['name']}"
                
                # 过滤 Day 1 特有阶段
                if step.get("day_limit") and d != step.get("day_limit"):
                    continue

                # 判定状态
                is_past = (d < state.day) or (d == state.day and self._is_step_before_current(phase_group, current_phase_group, canonical_flow))
                is_current = (d == state.day and phase_group == current_phase_group)
                is_future = not (is_past or is_current)
                
                if is_future and d > state.day + 1: continue

                # 判定跳过逻辑 (状态机判定该组是否在未来会被跳过)
                if not is_past and machine.check_skip(phase_group):
                    status_icon = "❌"
                    detail = "[SKIPPED: Role Eliminated]"
                else:
                    if is_past:
                        status_icon = "✅"
                        detail = self._get_historical_detail(phase_group, d, state)
                    elif is_current:
                        status_icon = "🔄"
                        detail = "[IN PROGRESS]"
                    else:
                        status_icon = "⏳"
                        detail = ""

                    # 判定角色适用性 (Eyes Closed)
                    if not self._is_phase_applicable_for_detail(phase_group):
                        status_icon = "😴"
                        detail = "[Eyes Closed]"

                tree += f"{status_icon} {name} {detail}\n"
        
        tree += "```\n\n"
        tree += "✅ Completed | 🔄 In Progress | ⏳ Not Started | 😴 Eyes Closed | ❌ Skipped (Role Dead)\n\n"
        return tree

    def _get_phase_group(self, phase: str) -> str:
        return (PHASE_CONFIG.get(phase) or {}).get("group", phase)

    def _is_step_before_current(self, step_phase, current_group, flow) -> bool:
        idx_map = {step["phase"]: i for i, step in enumerate(flow)}
        return idx_map.get(step_phase, 0) < idx_map.get(current_group, 0)

    def _get_historical_detail(self, phase_group: str, day: int, state: AgentGameState) -> str:
        """从历史 events 中聚合详情"""
        details = []
        for event in state.events:
            event_group = (PHASE_CONFIG.get(event.get("status")) or {}).get("group")
            if event_group == phase_group and event.get("round") == day:
                content = event.get("content", "")
                if not content: continue
                
                trace_parts = []
                for t in event.get("traces", []):
                    f = self._add_you_marker(t.get("from", "?"))
                    to = t.get("to")
                    act = t.get("action", "")
                    if to:
                        trace_parts.append(f"{f} --[{act}]--> {self._add_you_marker(to)}")
                    else:
                        trace_parts.append(f"{f} ({act})")
                
                trace_str = f" | Traces: {', '.join(trace_parts)}" if trace_parts else ""
                details.append(f"    └─ {content}{trace_str}")
        
        return "\n" + "\n".join(details) if details else ""

    def _is_phase_applicable_for_detail(self, phase_group: str) -> bool:
        """根据大阶段组判断可见性"""
        for p, cfg in PHASE_CONFIG.items():
            if cfg.get("group") == phase_group:
                if cfg["is_global"] or self.agent_role in cfg["applicable_roles"]:
                    return True
        return False

    def _add_you_marker(self, player_id: str) -> str:
        if str(player_id) == str(self.agent_id):
            return f"{player_id}(You)"
        return str(player_id)
