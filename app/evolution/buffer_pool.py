"""evolution/buffer_pool.py — 策略建议缓冲池（MySQL 持久化）

职责：
  1. 接收反思引擎产出的建议
  2. 按场景标签路由到 pending 或 cluster
  3. 管理建议生命周期（过期清理）
"""
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Any

from sqlalchemy import func

from evolution.config import EvolutionConfig
from evolution.db import get_session
from evolution.models import EvolutionBufferItem
from evolution.reflection_engine import ReflectionResult


class BufferPool:
    def __init__(self, cfg: EvolutionConfig):
        self.cfg = cfg

    def ingest(self, result: ReflectionResult) -> str:
        """接收一条反思结果，写入缓冲池。"""
        sug = result.suggestion

        if sug.match_level in ("low", "strategy_gap"):
            return "skipped"

        payload = self._result_to_dict(result)
        preview_texts = []
        text = (payload.get("suggestion") or {}).get("text", "")
        if text:
            preview_texts.append(text[:80])

        session = get_session()
        try:
            item = EvolutionBufferItem(
                item_type="pending",
                item_key=result.suggestion_id,
                target_skill_name=sug.target_skill,
                suggestion_count=1,
                avg_causal_strength=sug.causal_strength,
                consistency_rate=1.0,
                scene_tags_json=payload.get("scene_tags", {}),
                preview_texts_json=preview_texts,
                payload_json=payload,
            )
            session.add(item)
            session.commit()
            return "buffered"
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def load_pending(self) -> List[Dict]:
        """加载所有 pending 建议。"""
        session = get_session()
        try:
            items = session.query(EvolutionBufferItem).filter_by(item_type="pending").all()
            return [item.payload_json for item in items]
        finally:
            session.close()

    def load_cluster(self, cluster_id: str) -> Optional[Dict]:
        """加载指定 cluster 的全部建议。"""
        session = get_session()
        try:
            item = session.query(EvolutionBufferItem).filter_by(
                item_type="cluster", item_key=cluster_id
            ).first()
            return item.payload_json if item else None
        finally:
            session.close()

    def list_clusters(self) -> List[str]:
        """列出所有 cluster ID。"""
        session = get_session()
        try:
            items = session.query(EvolutionBufferItem).filter_by(item_type="cluster").all()
            return [item.item_key for item in items]
        finally:
            session.close()

    def save_cluster(self, cluster_id: str, data: Dict):
        """创建或更新 cluster。"""
        suggestions = data.get("suggestions", [])
        preview_texts = []
        for s in suggestions[:3]:
            text = (s.get("suggestion") or {}).get("text", "")
            if text:
                preview_texts.append(text[:80])

        session = get_session()
        try:
            item = session.query(EvolutionBufferItem).filter_by(
                item_type="cluster", item_key=cluster_id
            ).first()
            if item:
                item.suggestion_count = len(suggestions)
                item.avg_causal_strength = data.get("avg_causal_strength", 0)
                item.consistency_rate = data.get("consistency_rate", 0)
                item.scene_tags_json = data.get("scene_tags", {})
                item.preview_texts_json = preview_texts
                item.target_skill_name = data.get("target_skill", "")
                item.payload_json = data
                item.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            else:
                item = EvolutionBufferItem(
                    item_type="cluster",
                    item_key=cluster_id,
                    cluster_id=cluster_id,
                    target_skill_name=data.get("target_skill", ""),
                    suggestion_count=len(suggestions),
                    avg_causal_strength=data.get("avg_causal_strength", 0),
                    consistency_rate=data.get("consistency_rate", 0),
                    scene_tags_json=data.get("scene_tags", {}),
                    preview_texts_json=preview_texts,
                    payload_json=data,
                )
                session.add(item)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def delete_pending(self, suggestion_id: str):
        """删除已处理的 pending 建议。"""
        session = get_session()
        try:
            item = session.query(EvolutionBufferItem).filter_by(
                item_type="pending", item_key=suggestion_id
            ).first()
            if item:
                session.delete(item)
                session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def move_to_confirmed(self, cluster_id: str):
        """将 cluster 移入 confirmed。"""
        session = get_session()
        try:
            item = session.query(EvolutionBufferItem).filter_by(
                item_type="cluster", item_key=cluster_id
            ).first()
            if item:
                item.item_type = "confirmed"
                item.status = "confirmed"
                item.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
                session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def expire_old_suggestions(self) -> int:
        """清理过期建议，返回清理数量。"""
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.cfg.buffer.max_age_days)
        cutoff_naive = cutoff.replace(tzinfo=None)

        session = get_session()
        try:
            items = session.query(EvolutionBufferItem).filter_by(item_type="pending").all()
            count = 0
            for item in items:
                if item.created_at and item.created_at < cutoff_naive:
                    item.item_type = "expired"
                    item.status = "expired"
                    count += 1
            if count:
                session.commit()
            return count
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_status(self) -> Dict:
        """返回缓冲池状态摘要。"""
        session = get_session()
        try:
            pending_count = session.query(func.count(EvolutionBufferItem.id)).filter_by(item_type="pending").scalar() or 0
            cluster_count = session.query(func.count(EvolutionBufferItem.id)).filter_by(item_type="cluster").scalar() or 0
            confirmed_count = session.query(func.count(EvolutionBufferItem.id)).filter_by(item_type="confirmed").scalar() or 0
            expired_count = session.query(func.count(EvolutionBufferItem.id)).filter_by(item_type="expired").scalar() or 0

            cluster_details = []
            clusters = session.query(EvolutionBufferItem).filter_by(item_type="cluster").all()
            for c in clusters:
                cluster_details.append({
                    "cluster_id": c.item_key,
                    "suggestion_count": c.suggestion_count,
                    "target_skill": c.target_skill_name or "",
                    "avg_causal_strength": float(c.avg_causal_strength or 0),
                })

            return {
                "pending_count": pending_count,
                "cluster_count": cluster_count,
                "confirmed_count": confirmed_count,
                "expired_count": expired_count,
                "clusters": cluster_details,
            }
        finally:
            session.close()

    def _result_to_dict(self, result: ReflectionResult) -> Dict:
        """将 ReflectionResult 转为可序列化的 dict。"""
        return {
            "suggestion_id": result.suggestion_id,
            "game_id": result.game_id,
            "my_role": result.my_role,
            "result": result.result,
            "scene_tags": {
                "role": result.scene_tags.role,
                "role_survived_rounds": result.scene_tags.role_survived_rounds,
                "sheriff_contested": result.scene_tags.sheriff_contested,
                "first_night_target": result.scene_tags.first_night_target,
                "wolf_aggression": result.scene_tags.wolf_aggression,
                "good_coordination": result.scene_tags.good_coordination,
                "critical_phase": result.scene_tags.critical_phase,
                "result": result.scene_tags.result,
                "death_cause": result.scene_tags.death_cause,
            },
            "causal_chain": [
                {
                    "action": step.action,
                    "intermediate": step.intermediate,
                    "outcome": step.outcome,
                    "is_strategy_driven": step.is_strategy_driven,
                    "is_luck_driven": step.is_luck_driven,
                }
                for step in result.causal_chain
            ],
            "suggestion": {
                "text": result.suggestion.text,
                "confidence": result.suggestion.confidence,
                "direction": result.suggestion.direction,
                "target_skill": result.suggestion.target_skill,
                "match_level": result.suggestion.match_level,
                "causal_strength": result.suggestion.causal_strength,
            },
            "in_game_flags": result.in_game_flags,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
