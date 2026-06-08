"""evolution/buffer_pool.py — 策略建议缓冲池（MySQL 持久化）

职责：
  1. 接收反思引擎产出的建议
  2. 按场景标签路由到 pending 或 cluster
  3. 管理建议生命周期（过期清理）
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Any

from sqlalchemy import func

from evolution.config import EvolutionConfig
from evolution.db import get_session
from evolution.models import EvolutionBufferItem
from evolution.reflection_engine import ReflectionResult

logger = logging.getLogger("evolution.buffer_pool")


class BufferPool:
    def __init__(self, cfg: EvolutionConfig):
        self.cfg = cfg

    def ingest(self, result: ReflectionResult) -> str:
        """接收一条反思结果，写入缓冲池。"""
        sug = result.suggestion

        if sug.match_level == "strategy_gap":
            logger.info(f"Ingest skipped: match_level=strategy_gap, target={sug.target_skill}")
            return "skipped"

        if sug.match_level == "low":
            # low 不再丢弃，打折 causal_strength 后允许进入管道
            sug.causal_strength *= 0.5
            logger.info(f"Ingest low→buffered with 0.5 causal discount: target={sug.target_skill}, original_causal={result.suggestion.causal_strength:.2f}, discounted={sug.causal_strength:.2f}")

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
            logger.info(f"Ingest buffered: id={item.id}, target={sug.target_skill}, match_level={sug.match_level}, causal={sug.causal_strength:.2f}")
            return "buffered"
        except Exception:
            session.rollback()
            logger.exception(f"Ingest failed: target={sug.target_skill}")
            raise
        finally:
            session.close()

    def load_pending(self) -> List[Dict]:
        """加载所有 pending 建议。

        返回的每条建议包含 _item_key 字段，用于 delete_pending 精确定位行。
        """
        session = get_session()
        try:
            items = session.query(EvolutionBufferItem).filter_by(item_type="pending").all()
            logger.debug(f"Loaded {len(items)} pending items")
            result = []
            for item in items:
                payload = item.payload_json or {}
                payload["_item_key"] = item.item_key
                result.append(payload)
            return result
        finally:
            session.close()

    def load_cluster(self, cluster_id: str) -> Optional[Dict]:
        """加载指定 cluster 的全部建议（cluster 或 confirmed 均可）。"""
        session = get_session()
        try:
            item = session.query(EvolutionBufferItem).filter(
                EvolutionBufferItem.item_key == cluster_id,
                EvolutionBufferItem.item_type.in_(["cluster", "confirmed"]),
            ).first()
            if not item:
                return None
            result = {
                "cluster_id": item.item_key,
                "suggestion_count": item.suggestion_count,
                "target_skill": item.target_skill_name or "",
                "avg_causal_strength": float(item.avg_causal_strength or 0),
                "consistency_rate": float(item.consistency_rate or 0),
                "scene_tags": item.scene_tags_json or {},
                "preview_texts": item.preview_texts_json or [],
                "created_at": item.created_at.isoformat() if item.created_at else None,
                "updated_at": item.updated_at.isoformat() if item.updated_at else None,
            }
            if item.payload_json and "suggestions" in item.payload_json:
                result["suggestions"] = item.payload_json["suggestions"]
            return result
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
                from sqlalchemy.orm.attributes import flag_modified
                flag_modified(item, "payload_json")
                item.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
                logger.info(f"Cluster updated: {cluster_id}, count={len(suggestions)}, causal={data.get('avg_causal_strength', 0):.2f}, consist={data.get('consistency_rate', 0):.2f}")
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
                logger.info(f"Cluster created: {cluster_id}, target={data.get('target_skill', '')}, count={len(suggestions)}")
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
                logger.debug(f"Pending deleted: {suggestion_id}")
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def move_to_confirmed(self, cluster_id: str):
        """将 cluster 移入 confirmed。

        幂等：若已存在同 item_key 的 confirmed 行（历史遗留或并发确认产生），
        则把新簇数据并入该行并删除重复的 cluster 行，避免翻转 item_type 时撞
        uk_evolution_buffer_item 唯一键导致整轮确认中断。
        """
        session = get_session()
        try:
            item = session.query(EvolutionBufferItem).filter_by(
                item_type="cluster", item_key=cluster_id
            ).first()
            if not item:
                return
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            existing = session.query(EvolutionBufferItem).filter_by(
                item_type="confirmed", item_key=cluster_id
            ).first()
            if existing and existing.id != item.id:
                existing.suggestion_count = max(existing.suggestion_count or 0,
                                                item.suggestion_count or 0)
                existing.avg_causal_strength = item.avg_causal_strength
                existing.consistency_rate = item.consistency_rate
                existing.scene_tags_json = item.scene_tags_json
                existing.preview_texts_json = item.preview_texts_json
                existing.payload_json = item.payload_json
                from sqlalchemy.orm.attributes import flag_modified
                flag_modified(existing, "payload_json")
                existing.target_skill_name = item.target_skill_name
                existing.updated_at = now
                session.delete(item)
                session.commit()
                logger.info(f"Cluster merged into existing confirmed (idempotent): {cluster_id}")
                return
            item.item_type = "confirmed"
            item.status = "confirmed"
            item.updated_at = now
            session.commit()
            logger.info(f"Cluster confirmed: {cluster_id}")
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def expire_old_suggestions(self) -> int:
        """清理过期建议 + 超龄单建议 cluster，返回清理数量。"""
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.cfg.buffer.max_age_days)
        cutoff_naive = cutoff.replace(tzinfo=None)

        session = get_session()
        try:
            # 1. 过期 pending 建议
            items = session.query(EvolutionBufferItem).filter_by(item_type="pending").all()
            count = 0
            for item in items:
                if item.created_at and item.created_at < cutoff_naive:
                    item.item_type = "expired"
                    item.status = "expired"
                    count += 1
            if count:
                logger.info(f"Expired {count} old pending suggestions (cutoff={cutoff_naive})")

            # 2. 过期超龄单建议 cluster（suggestion_count=1 且超过 max_age_days）
            stale_clusters = session.query(EvolutionBufferItem).filter_by(
                item_type="cluster"
            ).filter(
                EvolutionBufferItem.suggestion_count <= 1,
                EvolutionBufferItem.created_at < cutoff_naive,
            ).all()
            stale_count = 0
            for item in stale_clusters:
                item.item_type = "expired"
                item.status = "expired"
                stale_count += 1
            if stale_count:
                logger.info(f"Expired {stale_count} stale single-suggestion clusters (cutoff={cutoff_naive})")

            if count or stale_count:
                session.commit()
            return count + stale_count
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
            confirmed_details = []
            for item_type, detail_list in [("cluster", cluster_details), ("confirmed", confirmed_details)]:
                items = session.query(EvolutionBufferItem).filter_by(item_type=item_type).all()
                for c in items:
                    detail_list.append({
                        "cluster_id": c.item_key,
                        "suggestion_count": c.suggestion_count,
                        "target_skill": c.target_skill_name or "",
                        "avg_causal_strength": float(c.avg_causal_strength or 0),
                        "consistency_rate": float(c.consistency_rate or 0),
                        "scene_tags": c.scene_tags_json or {},
                        "preview_texts": c.preview_texts_json or [],
                        "created_at": c.created_at.isoformat() if c.created_at else None,
                        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
                    })

            return {
                "pending_count": pending_count,
                "cluster_count": cluster_count,
                "confirmed_count": confirmed_count,
                "expired_count": expired_count,
                "clusters": cluster_details,
                "confirmed_clusters": confirmed_details,
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
