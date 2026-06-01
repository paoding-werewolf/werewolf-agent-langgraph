"""evolution/in_game_flagger.py — 对局中即时标记

通过 prompt 注入让 Agent 在执行策略时发现矛盾并标记。
标记不修改策略，只附加到当前对局 trace 中，
等对局结束后由反思引擎处理。
"""
from typing import List, Dict, Any


IN_GAME_FLAG_PROMPT = """
当你执行策略时发现策略与实际局势不符（例如策略前提不成立、分支缺失、推荐行动明显不合理）：
1. 不要惊慌，先按当前最佳判断继续行动
2. 在你的思考过程中标记："[FLAG] 策略 X 与当前局势矛盾：原因 Y"
3. 这个标记会被自动记录，在对局结束后用于策略优化

重要：不要在对局中试图大幅修改你的策略思路，保持行为一致性。

同时，在执行决策前评估当前局势与已有策略的匹配度：
- 高匹配（策略明确覆盖当前场景）→ 按策略执行
- 中匹配（策略部分覆盖，局势有差异）→ 策略参考 + 现场调整
- 低匹配 / 策略无覆盖 → 独立判断，不要被动跟风或沉默
"""


class InGameFlagger:
    """从 Agent 的思考过程中提取即时标记。"""

    FLAG_PATTERN = r'\[FLAG\]\s*(.+?)(?=\n|$)'

    def extract_flags(self, thought_text: str) -> List[Dict[str, Any]]:
        """从 Agent 的思考文本中提取 [FLAG] 标记。"""
        import re
        flags = []
        for match in re.finditer(self.FLAG_PATTERN, thought_text):
            flags.append({
                "type": "strategy_mismatch",
                "description": match.group(1).strip(),
                "detected_in": "thought",
            })
        return flags

    def get_prompt_injection(self) -> str:
        """返回要注入到 act prompt 中的指令文本。"""
        return IN_GAME_FLAG_PROMPT
