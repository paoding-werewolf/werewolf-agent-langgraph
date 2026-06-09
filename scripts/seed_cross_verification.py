"""种子脚本：插入好人阵营交叉验证策略（幂等，已存在则跳过）。

部署后执行一次：
  cd werewolf-agent-langgraph && python scripts/seed_cross_verification.py
"""
import sys
sys.path.insert(0, ".")

from evolution.db import get_session
from evolution.models import EvolutionSkill

STRATEGY = """---
name: 好人阵营交叉验证
role: common
tags: [logic, analysis, verification, speech, vote, strategy]
---

## When to Use
作为好人阵营，你需要从多个独立来源验证关键结论。

## Strategy

1. **多源验证**
   - 同一结论如果同时被"查验结果""投票记录""发言矛盾"三者中的至少两者支持，可信度大幅提升
   - 反之，如果某个结论只有单一信息源支撑（如某个玩家的个人推测），应保持怀疑

2. **公开可追溯**
   - 你提出的每个论点应该能在公开事件中找到对应证据
   - 不要只依赖你的私有信息做判断——好人无法验证你的私有信息
   - 如果你掌握私有信息可以与公开信息互补，这是信任该推理方向的信号

3. **信息互补**
   - 当有玩家声称自己是神职时：检验其声称是否与公开事件一致（如"我是女巫"→是否有人在讨论中暗示银水信息）
   - 如果多个好人从不同角度得出了相同结论，这比一个人的推理更可靠

4. **质疑一致性**
   - 如果某人的发言与他之前的投票记录矛盾，这是高度可疑的信号
   - 如果某人的怀疑方向突然间大幅转变且未给出充分理由，需要审视其动机
   - 多人同时改变对同一目标的立场，可能是狼队协调的信号

## Constraints
- 交叉验证的目的是减少误判——不是增加决策复杂度
- 当你掌握关键私有信息时，应该在发言中隐晦引导，而非直接暴露
"""


def seed():
    session = get_session()
    try:
        existing = session.query(EvolutionSkill).filter_by(
            skill_name="好人阵营交叉验证"
        ).first()
        if existing:
            print(f"[SKIP] 好人阵营交叉验证 already exists (default={existing.current_default})")
        else:
            from evolution.skill_loader import SkillLoader
            from evolution.config import load_config
            cfg = load_config()
            loader = SkillLoader(cfg)
            version = loader.create_new_version(
                skill_name="好人阵营交叉验证",
                content=STRATEGY,
                source="bundled",
                role="common",
            )
            print(f"[OK] 好人阵营交叉验证 created as {version}")

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
