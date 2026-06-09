"""种子脚本：插入狼人白天协作 + 警长归票策略（幂等，已存在则跳过）。

部署后执行一次即可：
  cd werewolf-agent-langgraph && python scripts/seed_daytime_strategies.py
"""
import sys
sys.path.insert(0, ".")

from evolution.db import get_session
from evolution.models import EvolutionSkill

WOLF_DAYTIME_COORDINATION = """---
name: wolf-daytime-coordination
role: wolf
tags: [coordination, deception, identity_management, speech, vote, strategy]
---

## When to Use
白天发言和投票阶段。用于狼队在白天隐式协调行动方向。

## Strategy

1. **归票跟随**
   - 若警长是狼队友：发言中隐晦呼应其归票方向，投票时跟票
   - 若警长是好人：根据队友发言倾向和场上局势，独立判断是否跟归票
   - 核心原则：可以跟票，但不能形成明显抱团

2. **互保边界**
   - 可为队友做轻度辩护（如"我觉得X号这轮发言还可以"），但必须是好人视角下合理的行为
   - 不得多头同时为同一可疑玩家辩护——这是最容易被识别为狼队抱团的信号
   - 辩护必须基于可公开验证的信息，不得引用夜间信息

3. **分票策略**
   - 若好人归票明确指向狼队友：2狼投归票目标、其余狼弃权或投其他目标
   - 避免全部狼投同一方向的极端票型
   - 分票时每人的弃权/投它理由必须在好人视角下自圆其说

4. **发言信号**
   - 通过"我倾向于投X号""X号这轮发言有几个矛盾"等标准话术传递意图
   - 禁止使用固定暗号、场外信息或非公开信息
   - 信号强度要弱到好人无法察觉异常

## Constraints
- 以上所有协调行为必须在好人视角下可解释
- 与狼人思维框架"投票一致性检查"不矛盾：抱团=所有人投同一方向=暴露；分票=分散投票=掩护；跟归票=合理=不暴露
- 本轮结束后回顾：你的投票和发言是否与前面的立场一致？
"""

SHERIFF_VOTE_CALL = """---
name: sheriff-vote-call
role: common
tags: [sheriff, vote, speech, persuasion, strategy]
---

## When to Use
你是警长且处于白天发言阶段（通常是最后一个发言）。

## Strategy

1. **归票义务**
   - 作为警长，轮到你发言时请在发言末尾给出明确的归票方向
   - 格式示例："我建议今天投票放逐X号，理由是..."
   - 归票必须有具体理由支撑（查验结果推断、投票轨迹异常、发言矛盾等），不能只说"投X号"

2. **前置分析**
   - 先总结场上讨论的主要分歧和关键疑点
   - 然后基于你的分析给出归票建议
   - 归票目标不能是你无法给出合理怀疑理由的玩家

3. **处理质疑**
   - 如果归票被其他玩家反驳或你的警长身份被质疑，给出回应
   - 但回应之后仍需坚持或调整归票方向——保持决策清晰，不要左右摇摆

4. **特殊情况**
   - 如果你无法确定归票方向（信息不足）：诚实说明，建议弃权或等下一轮
   - 如果你自己就是嫌疑人：先做自我辩护，再给出归票建议
   - 注意：归票只是建议，其他玩家会根据自身信息判断是否跟票

## Constraints
- 你的归票对好人有影响力——归错的责任在你
- 狼人警长也可以归票误导好人——这是游戏的一部分
- 不要因为担心归错而不归票——不归票对好人没有任何帮助
"""


def seed():
    session = get_session()
    try:
        # ── 狼人白天协作 ──
        existing = session.query(EvolutionSkill).filter_by(
            skill_name="wolf-daytime-coordination"
        ).first()
        if existing:
            print(f"[SKIP] wolf-daytime-coordination already exists (default={existing.current_default})")
        else:
            from evolution.skill_loader import SkillLoader
            from evolution.config import load_config
            cfg = load_config()
            loader = SkillLoader(cfg)
            version = loader.create_new_version(
                skill_name="wolf-daytime-coordination",
                content=WOLF_DAYTIME_COORDINATION,
                source="bundled",
                role="wolf",
            )
            print(f"[OK] wolf-daytime-coordination created as {version}")

        # ── 警长归票 ──
        existing = session.query(EvolutionSkill).filter_by(
            skill_name="sheriff-vote-call"
        ).first()
        if existing:
            print(f"[SKIP] sheriff-vote-call already exists (default={existing.current_default})")
        else:
            from evolution.skill_loader import SkillLoader
            from evolution.config import load_config
            cfg = load_config()
            loader = SkillLoader(cfg)
            version = loader.create_new_version(
                skill_name="sheriff-vote-call",
                content=SHERIFF_VOTE_CALL,
                source="bundled",
                role="common",
            )
            print(f"[OK] sheriff-vote-call created as {version}")

        session.commit()
        print("Done.")
    except Exception as e:
        session.rollback()
        print(f"Error: {e}")
        raise
    finally:
        session.close()


if __name__ == "__main__":
    seed()
