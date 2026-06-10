"""一次性迁移：将策略名从英文翻译为中文"""
import sys
sys.path.insert(0, "/app")

from evolution.db import get_session
from evolution.models import EvolutionSkill, EvolutionBufferItem

TRANSLATIONS = {
    "seer-identity-timing": "预言家身份跳明时机",
    "guard-priority-sequence": "守卫优先保护顺序",
    "villager-critical-thinking": "村民逻辑推理心法",
    "hunter-identity-reveal-timing": "猎人身份亮明时机",
    "witch-information-verification": "女巫信息甄别术",
    "wolf-independent-identity-with-info-disruption": "狼人独狼信息扰乱术",
    "seer-day1-target-priority": "预言家首日查验优先级",
    "wolf-flexible-alignment": "狼人灵活站边术",
    "deep-cover-wolf-coordination": "深水狼协同作战",
    "early-game-villager-survival": "村民前期保命指南",
    "seer-claim-precision": "预言家精准起跳术",
    "seer-mechanics-conflict-resolution": "预言家机制冲突化解",
    "witch-poison-timing": "女巫毒药投放时机",
    "witch-sheriff-contest": "女巫警徽争夺术",
    "wolf-counter-check-vote-control": "狼人反查验控票术",
    "guard-identity-claiming": "守卫身份伪装术",
    "wolf-teammate-distance-control": "狼人队友距离把控",
    "seer-sheriff-emergency-transfer": "预言家警徽紧急转移",
    "wolf-deep-undercover-attack-timing": "深水狼最佳出击时机",
    "wolf-king-last-words-deception": "狼王遗言诡计",
}

def migrate():
    session = get_session()
    try:
        # 1. 更新 evolution_skills.skill_name
        skills = session.query(EvolutionSkill).all()
        for sk in skills:
            if sk.skill_name in TRANSLATIONS:
                old_name = sk.skill_name
                new_name = TRANSLATIONS[old_name]
                print(f"Skill: {old_name} → {new_name}")
                sk.skill_name = new_name
        session.flush()

        # 2. 更新 evolution_buffer_items.target_skill_name + payload_json
        buffer_items = session.query(EvolutionBufferItem).all()
        updated_buffers = 0
        for item in buffer_items:
            if item.target_skill_name and item.target_skill_name in TRANSLATIONS:
                old = item.target_skill_name
                item.target_skill_name = TRANSLATIONS[old]
                # 同时更新 payload_json 中的 target_skill
                if item.payload_json and "target_skill" in item.payload_json:
                    from sqlalchemy.orm.attributes import flag_modified
                    item.payload_json["target_skill"] = TRANSLATIONS[old]
                    flag_modified(item, "payload_json")
                updated_buffers += 1
        print(f"Buffer items updated: {updated_buffers}")

        session.commit()
        print("Migration complete!")
    except Exception as e:
        session.rollback()
        print(f"Error: {e}")
        raise
    finally:
        session.close()

if __name__ == "__main__":
    migrate()
