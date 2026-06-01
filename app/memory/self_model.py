"""memory/self_model.py — 自我画像

存储：~/.werewolf-agent/memory/self_model/profile.yaml
加载：始终加载到 system prompt（精简版 ~500 tokens）
更新：每局结束后增量更新
"""
import re
import yaml
from pathlib import Path
from typing import Dict, Optional
from datetime import datetime, timezone

from evolution.config import AGENT_HOME

SELF_MODEL_PATH = AGENT_HOME / "memory" / "self_model" / "profile.yaml"


def load_self_model() -> Optional[Dict]:
    if SELF_MODEL_PATH.exists():
        with open(SELF_MODEL_PATH) as f:
            return yaml.safe_load(f)
    return None


def format_self_model_for_prompt() -> str:
    """格式化为 prompt 片段。"""
    model = load_self_model()
    if not model:
        return ""

    lines = ["## Self Profile (Your Historical Performance)"]

    role_stats = model.get("role_win_rates", {})
    if role_stats:
        rates = ", ".join(f"{r}: {v:.0%}" for r, v in role_stats.items())
        lines.append(f"Win Rates: {rates}")

    mistakes = model.get("common_mistakes", [])
    if mistakes:
        lines.append(f"Common Mistakes: {'; '.join(mistakes[:3])}")

    strengths = model.get("strengths", [])
    if strengths:
        lines.append(f"Strengths: {'; '.join(strengths[:3])}")

    return "\n".join(lines)


UPDATE_PROMPT = """根据本局表现更新 Agent 的自我画像。

我的角色: {my_role}
结果: {result}
关键决策回顾:
{key_decisions}

当前画像:
{current_profile}

输出更新后的 YAML:
```yaml
total_games: <累计>
role_win_rates:
  seer: <0-1>
  wolf: <0-1>
  witch: <0-1>
common_mistakes:
  - "<失误模式1>"
  - "<失误模式2>"
strengths:
  - "<强项1>"
improvement_areas:
  - "<改进方向1>"
recent_form: <最近5局的胜负列表>
```"""


def update_self_model(my_role: str, result: str,
                       key_decisions: str, llm_caller) -> bool:
    """对局结束后增量更新自我画像。"""
    current = load_self_model()
    current_text = yaml.dump(current, allow_unicode=True) if current else "（无历史画像）"

    prompt = UPDATE_PROMPT.format(
        my_role=my_role, result=result,
        key_decisions=key_decisions, current_profile=current_text,
    )

    try:
        resp = llm_caller.client.chat.completions.create(
            model=llm_caller.model,
            messages=[
                {"role": "system", "content": "你是 AI 玩家自我评估专家。输出 YAML。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        text = resp.choices[0].message.content or ""
        match = re.search(r'```(?:yaml)?\s*\n(.*?)```', text, re.DOTALL)
        yaml_text = match.group(1) if match else text

        updated = yaml.safe_load(yaml_text)
        if updated:
            SELF_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(SELF_MODEL_PATH, "w") as f:
                yaml.dump(updated, f, allow_unicode=True, default_flow_style=False)
            return True
    except Exception:
        pass
    return False
