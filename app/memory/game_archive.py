"""memory/game_archive.py — 对局历史归档（MySQL 持久化）

存储：evolution_game_archive + evolution_strategy_gaps 表
加载：按需检索
更新：每局结束后写入
"""
from typing import List, Dict, Optional
from datetime import datetime, timezone

from evolution.db import get_session
from evolution.models import EvolutionGameArchive, EvolutionStrategyGap


def save_game(game_id: str, my_role: str, result: str, day_count: int,
              scene_tags: Dict, reflection_report: str, full_trace: str,
              strategies_used: List[str],
              versions_used: Optional[Dict[str, str]] = None):
    """保存一局对局记录。"""
    has_builtin_ai = False

    payload = {
        "scene_tags": scene_tags,
        "reflection_report": reflection_report,
        "full_trace": full_trace,
        "strategies_used": strategies_used,
        "versions_used": versions_used or {},
    }

    session = get_session()
    try:
        record = session.query(EvolutionGameArchive).filter_by(game_id=game_id).first()
        if record:
            record.my_role = my_role
            record.result = result
            record.day_count = day_count
            record.has_builtin_ai = has_builtin_ai
            record.payload_json = payload
        else:
            session.add(EvolutionGameArchive(
                game_id=game_id,
                room_id=game_id,
                my_role=my_role,
                result=result,
                day_count=day_count,
                has_builtin_ai=has_builtin_ai,
                payload_json=payload,
            ))
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def record_strategy_gap(game_id: str, scene_description: str):
    """记录一次 strategy_gap（策略覆盖空白）。"""
    session = get_session()
    try:
        record = session.query(EvolutionStrategyGap).filter_by(
            scene_description=scene_description
        ).first()
        if record:
            record.gap_count += 1
            record.payload_json = {"game_id": game_id}
        else:
            session.add(EvolutionStrategyGap(
                scene_description=scene_description,
                gap_count=1,
                payload_json={"game_id": game_id},
            ))
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_frequent_gaps(min_count: int = 5) -> List[Dict]:
    """获取频繁出现的 strategy_gap（>= min_count 次）。"""
    session = get_session()
    try:
        rows = session.query(EvolutionStrategyGap).filter(
            EvolutionStrategyGap.gap_count >= min_count
        ).order_by(EvolutionStrategyGap.gap_count.desc()).all()
        return [
            {
                "id": row.id,
                "scene_description": row.scene_description,
                "gap_count": row.gap_count,
                "last_game_id": (row.payload_json or {}).get("game_id"),
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
            for row in rows
        ]
    finally:
        session.close()


# ── Role / phase display names ──

_ROLE_NAMES = {
    "werewolf": "狼人", "wolf": "狼人",
    "villager": "村民",
    "seer": "预言家",
    "witch": "女巫",
    "hunter": "猎人",
    "guard": "守卫",
    "idiot": "白痴",
    "wolf_king": "狼王",
}

_PHASE_NAMES = {
    "first_day_speech": "首日发言",
    "first_vote": "首日投票",
    "day_speech": "白天发言",
    "day_vote": "白天投票",
    "night_hunt": "夜间猎杀",
    "night_save": "夜间救援",
    "night_check": "夜间查验",
    "night_guard": "夜间守护",
    "mid_game": "中盘博弈",
    "end_game": "终局对决",
    "last_words": "遗言阶段",
}


def _parse_scene(scene_description: str) -> Dict:
    """Parse scene_description like 'villager_first_day_speech' into role + phase."""
    parts = scene_description.split("_", 1)
    role_key = parts[0] if parts else scene_description
    phase_key = parts[1] if len(parts) > 1 else ""
    return {
        "role_key": role_key,
        "role_name": _ROLE_NAMES.get(role_key, role_key),
        "phase_key": phase_key,
        "phase_name": _PHASE_NAMES.get(phase_key, phase_key),
    }


def get_gap_detail(gap_id: int) -> Optional[Dict]:
    """Get full detail for a single strategy gap."""
    session = get_session()
    try:
        row = session.query(EvolutionStrategyGap).filter_by(id=gap_id).first()
        if not row:
            return None

        parsed = _parse_scene(row.scene_description)
        last_game_id = (row.payload_json or {}).get("game_id")

        # Fetch the last triggering game from archive
        last_game = None
        if last_game_id:
            ga = session.query(EvolutionGameArchive).filter_by(game_id=last_game_id).first()
            if ga:
                payload = ga.payload_json or {}
                scene_tags = payload.get("scene_tags", {})
                last_game = {
                    "game_id": ga.game_id,
                    "room_id": ga.room_id,
                    "my_role": ga.my_role,
                    "result": ga.result,
                    "day_count": ga.day_count,
                    "created_at": ga.created_at.isoformat() if ga.created_at else None,
                    "scene_tags": scene_tags,
                    "strategies_used": payload.get("strategies_used", []),
                    "reflection_report": payload.get("reflection_report", "")[:2000],
                }

        # Find related buffer items (reflections) that flagged this same role+phase
        related_items = []
        try:
            from evolution.models import EvolutionBufferItem
            # Look for buffer items whose scene contains the same role
            items = (
                session.query(EvolutionBufferItem)
                .filter(EvolutionBufferItem.status == "pending")
                .order_by(EvolutionBufferItem.created_at.desc())
                .limit(20)
                .all()
            )
            for item in items:
                payload = item.payload_json or {}
                scene = payload.get("scene_tags", {})
                if scene.get("role") == parsed["role_key"]:
                    related_items.append({
                        "target_skill": item.target_skill_name,
                        "match_level": item.match_level,
                        "causal_strength": item.causal_strength,
                        "created_at": item.created_at.isoformat() if item.created_at else None,
                    })
        except Exception:
            pass

        return {
            "id": row.id,
            "scene_description": row.scene_description,
            "gap_count": row.gap_count,
            "role": parsed,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "last_game": last_game,
            "related_reflections": related_items[:5],
        }
    finally:
        session.close()
