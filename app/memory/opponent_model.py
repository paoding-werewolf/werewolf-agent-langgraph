"""memory/opponent_model.py — 跨局对手行为画像

存储：每个对手一个 YAML 文件 (~/.werewolf-agent/memory/opponents/{player_id}.yaml)
加载：开局时按本桌玩家 ID 检索
更新：每局结束后增量更新
"""
import re
import yaml
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime, timezone

from evolution.config import AGENT_HOME


OPPONENTS_DIR = AGENT_HOME / "memory" / "opponents"


def load_opponent(player_id: str) -> Optional[Dict]:
    """加载指定对手的画像。"""
    f = OPPONENTS_DIR / f"{player_id}.yaml"
    if f.exists():
        with open(f) as fh:
            return yaml.safe_load(fh)
    return None


def load_opponents_for_table(player_ids: List[str]) -> Dict[str, Dict]:
    """加载本桌所有对手的画像。"""
    result = {}
    for pid in player_ids:
        data = load_opponent(pid)
        if data:
            result[pid] = data
    return result


def format_opponents_for_prompt(opponents: Dict[str, Dict]) -> str:
    """将对手画像格式化为 prompt 片段。"""
    if not opponents:
        return ""

    lines = ["## Opponent Profiles"]
    for pid, data in opponents.items():
        lines.append(f"\n### Player {pid}")
        lines.append(f"- Games: {data.get('total_games', 0)} | Win Rate: {data.get('win_rate', 0):.0%}")

        wolf = data.get("wolf_behavior", {})
        if wolf:
            lines.append(f"- Wolf: bluff_rate={wolf.get('bluff_rate', 'N/A')}, kill_pref={wolf.get('kill_preference', 'N/A')}")

        good = data.get("good_behavior", {})
        if good:
            lines.append(f"- Good: vote_independence={good.get('vote_independence', 'N/A')}")

        weaknesses = data.get("weaknesses", [])
        if weaknesses:
            lines.append(f"- Weaknesses: {'; '.join(weaknesses[:3])}")

    return "\n".join(lines)


UPDATE_PROMPT = """分析以下对手在本局中的行为，更新其画像。

对手 ID: {player_id}
本局角色: {role}
本局行为摘要:
{behavior_summary}

当前画像:
{current_profile}

输出更新后的完整 YAML（保持相同结构，只更新数据）：
```yaml
player_id: "{player_id}"
total_games: <累计对局数>
last_seen: <今天日期 ISO 格式>
win_rate: <总胜率 0-1>
wolf_behavior:
  bluff_rate: <悍跳频率 0-1>
  kill_preference: <首夜刀人偏好描述>
  reaction_when_suspected: <被怀疑时的反应>
good_behavior:
  seer_jump_timing: <预言家跳身份时机>
  vote_independence: <投票独立性 0-1>
weaknesses:
  - <可利用弱点1>
  - <可利用弱点2>
recent_games:
  - <最近3局的简要记录>
```"""


def update_opponent_from_game(player_id: str, role: str,
                                behavior_summary: str,
                                llm_caller) -> bool:
    """对局结束后增量更新对手画像。"""
    current = load_opponent(player_id)
    current_text = yaml.dump(current, allow_unicode=True) if current else "（无历史画像）"

    prompt = UPDATE_PROMPT.format(
        player_id=player_id,
        role=role,
        behavior_summary=behavior_summary,
        current_profile=current_text,
    )

    try:
        resp = llm_caller.client.chat.completions.create(
            model=llm_caller.model,
            messages=[
                {"role": "system", "content": "你是狼人杀玩家行为分析专家。输出 YAML。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        text = resp.choices[0].message.content or ""

        match = re.search(r'```(?:yaml)?\s*\n(.*?)```', text, re.DOTALL)
        yaml_text = match.group(1) if match else text

        updated = yaml.safe_load(yaml_text)
        if updated and isinstance(updated, dict):
            OPPONENTS_DIR.mkdir(parents=True, exist_ok=True)
            with open(OPPONENTS_DIR / f"{player_id}.yaml", "w") as f:
                yaml.dump(updated, f, allow_unicode=True, default_flow_style=False)
            return True
    except Exception:
        pass
    return False
