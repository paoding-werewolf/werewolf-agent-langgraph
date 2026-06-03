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
              strategies_used: List[str]):
    """保存一局对局记录。"""
    has_builtin_ai = False

    payload = {
        "scene_tags": scene_tags,
        "reflection_report": reflection_report,
        "full_trace": full_trace,
        "strategies_used": strategies_used,
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
            {"scene_description": row.scene_description, "gap_count": row.gap_count}
            for row in rows
        ]
    finally:
        session.close()