"""evolution/reflection_engine.py — 结构化反思引擎

输入：完整 game trace + 即时标记 + 工作记忆
输出：ReflectionResult（因果链 + 策略建议 + 场景标签 + 置信度）
"""
import json
import logging
import re
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone

from agents.llm_caller import LLMCaller
from evolution.config import EvolutionConfig

logger = logging.getLogger("evolution.reflection")


# ── 数据结构 ────────────────────────────────────────────────

@dataclass
class CausalStep:
    action: str
    intermediate: str
    outcome: str
    is_strategy_driven: bool = True
    is_luck_driven: bool = False


@dataclass
class SceneTags:
    role: str = ""
    role_survived_rounds: int = 0
    sheriff_contested: bool = False
    first_night_target: str = ""
    wolf_aggression: str = "medium"
    good_coordination: str = "medium"
    critical_phase: str = ""
    result: str = ""
    death_cause: str = ""


@dataclass
class StrategySuggestion:
    text: str = ""
    confidence: float = 0.5
    direction: str = "modify"
    target_skill: str = ""
    match_level: str = "high"
    causal_strength: float = 0.5


@dataclass
class ReflectionResult:
    suggestion_id: str
    game_id: str
    my_role: str
    result: str
    scene_tags: SceneTags = field(default_factory=SceneTags)
    causal_chain: List[CausalStep] = field(default_factory=list)
    suggestion: StrategySuggestion = field(default_factory=StrategySuggestion)
    in_game_flags: List[Dict] = field(default_factory=list)


# ── 反思 Prompt ─────────────────────────────────────────────

REFLECT_SYSTEM_PROMPT = """你是一个狼人杀策略分析专家。你的任务是回顾一局完整的对局记录，
进行深度因果分析，并产出结构化的策略建议。

你必须严格区分：
- 策略导致的后果（因为某个决策引发了可追踪的因果链）
- 运气导致的后果（随机事件、对手不可预测的行为）

输出必须是合法的 YAML 格式，不要包含多余文本。"""

REFLECT_USER_TEMPLATE = """## 对局信息
- 房间: {game_id}
- 我的角色: {my_role}
- 我的座位: {my_seat}
- 结果: {result}

## 完整对局 Trace
{game_trace}

## 对局中的即时标记（如果有）
{in_game_flags}

## 工作记忆（本局积累的结构化信息）
{working_memory_text}

## 当前使用的策略
{current_strategies}

## 请输出以下 YAML 结构

```yaml
scene_tags:
  role: {my_role}
  role_survived_rounds: <我存活了几轮，整数>
  sheriff_contested: <是否有警长对跳，true/false>
  first_night_target: <首夜被刀者角色，如 "seer"/"villager"/"none">
  wolf_aggression: <high/medium/low>
  good_coordination: <high/medium/low>
  critical_phase: <first_day_speech/first_vote/mid_game/end_game>
  result: <won/lost>
  death_cause: <wolf_kill/vote_out/poison/shoot/guard_paradox/none>

causal_chain:
  - action: "<我做了什么关键决策>"
    intermediate: "<导致了什么中间结果>"
    outcome: "<最终产生了什么后果>"
    is_strategy_driven: <true/false>
    is_luck_driven: <true/false>

suggestion:
  text: "<一句话策略建议>"
  confidence: <0-1，你自评这条建议有多可靠>
  direction: <create/modify/discard>
  target_skill: "<目标策略名，如 seer-identity-timing>"
  match_level: <high/medium/low>

causal_strength: <0-1，本局中该建议与结果之间的因果关联强度>
```

注意事项：
1. causal_strength 和 confidence 是两个独立维度：
   - confidence: 你觉得这条建议有多正确（主观）
   - causal_strength: 本局的输赢与你的策略选择有多大因果关系（客观）
2. 如果这局输赢主要靠运气，causal_strength 应该低
3. match_level = low 的建议不会进入策略更新管道，只标记为 strategy_gap
4. 如果没什么值得修改的，direction 填 discard"""


class ReflectionEngine:
    def __init__(self, cfg: EvolutionConfig, llm_caller: Optional[LLMCaller] = None):
        self.cfg = cfg
        self.llm = llm_caller or LLMCaller()
        if cfg.reflection_model:
            self.reflect_llm = LLMCaller()
            self.reflect_llm.model = cfg.reflection_model
        else:
            self.reflect_llm = self.llm

    def reflect(self,
                game_id: str,
                my_role: str,
                my_seat: str,
                result: str,
                game_trace: str,
                in_game_flags: List[Dict],
                current_strategies: str = "",
                working_memory_text: str = "") -> Optional[ReflectionResult]:
        """执行对局后反思。"""
        flags_text = json.dumps(in_game_flags, ensure_ascii=False, indent=2) if in_game_flags else "（无即时标记）"

        user_msg = REFLECT_USER_TEMPLATE.format(
            game_id=game_id,
            my_role=my_role,
            my_seat=my_seat,
            result=result,
            game_trace=game_trace,
            in_game_flags=flags_text,
            working_memory_text=working_memory_text or "（无工作记忆）",
            current_strategies=current_strategies or "（无策略文档）",
        )

        response = self._call_llm(user_msg)

        parsed = self._parse_reflection_yaml(response)
        if not parsed:
            logger.warning(f"Reflection YAML parse failed for game={game_id}, raw response length={len(response)}, first 200 chars: {response[:200]}")
            return None

        suggestion_id = f"sug_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{game_id}"

        scene = SceneTags(**{k: v for k, v in parsed.get("scene_tags", {}).items()
                            if k in SceneTags.__dataclass_fields__})

        causal_chain = []
        for step in parsed.get("causal_chain", []):
            causal_chain.append(CausalStep(**{k: v for k, v in step.items()
                                              if k in CausalStep.__dataclass_fields__}))

        sug_data = parsed.get("suggestion", {})
        suggestion = StrategySuggestion(**{k: v for k, v in sug_data.items()
                                           if k in StrategySuggestion.__dataclass_fields__})

        suggestion.causal_strength = float(parsed.get("causal_strength", 0.5))

        if in_game_flags and suggestion.causal_strength > 0:
            suggestion.causal_strength = min(
                1.0,
                suggestion.causal_strength * self.cfg.in_game_flag_causal_multiplier
            )

        if suggestion.match_level == "medium":
            suggestion.causal_strength *= self.cfg.medium_match_causal_discount

        logger.info(f"Reflection success: game={game_id}, role={my_role}, result={result}, target_skill={suggestion.target_skill}, match_level={suggestion.match_level}, direction={suggestion.direction}, causal={suggestion.causal_strength:.2f}, confidence={suggestion.confidence:.2f}")

        return ReflectionResult(
            suggestion_id=suggestion_id,
            game_id=game_id,
            my_role=my_role,
            result=result,
            scene_tags=scene,
            causal_chain=causal_chain,
            suggestion=suggestion,
            in_game_flags=in_game_flags,
        )

    def _call_llm(self, user_msg: str) -> str:
        """调用反思用的 LLM。"""
        client = self.reflect_llm.client
        try:
            resp = client.chat.completions.create(
                model=self.reflect_llm.model,
                messages=[
                    {"role": "system", "content": REFLECT_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.3,
            )
            content = resp.choices[0].message.content or ""
            logger.debug(f"Reflection LLM response length={len(content)}")
            return content
        except Exception as e:
            logger.error(f"Reflection LLM call failed: {e}")
            return f"ERROR: {e}"

    def _parse_reflection_yaml(self, text: str) -> Optional[Dict]:
        """从 LLM 输出中提取 YAML 并解析。"""
        import yaml as _yaml

        match = re.search(r'```(?:yaml)?\s*\n(.*?)```', text, re.DOTALL)
        if match:
            yaml_text = match.group(1)
        else:
            yaml_text = text

        try:
            result = _yaml.safe_load(yaml_text)
            if not isinstance(result, dict):
                logger.warning(f"Reflection YAML parsed but result is {type(result).__name__}, not dict")
                return None
            return result
        except Exception as e:
            logger.warning(f"Reflection YAML parse error: {e}, yaml_text length={len(yaml_text)}, first 300 chars: {yaml_text[:300]}")
            return None


# ── Trace 格式化工具 ─────────────────────────────────────────

def format_game_trace(events: List[Dict], players: Dict) -> str:
    """将 AgentState 中的 events 列表格式化为可读的对局 trace。"""
    lines = []
    current_round = 0

    for event in events:
        r = event.get("round", 1)
        status = event.get("status", "")
        content = event.get("content", "")
        traces = event.get("traces", [])

        if r != current_round:
            current_round = r
            lines.append(f"\n--- Round {r} ---")

        trace_parts = []
        for t in traces:
            f = t.get("from", "?")
            to = t.get("to", "")
            act = t.get("action", "")
            trace_parts.append(f"{f}→{to}({act})" if to else f"{f}({act})")

        trace_str = " | " + ", ".join(trace_parts) if trace_parts else ""
        lines.append(f"[{status}] {content}{trace_str}")

    return "\n".join(lines)
