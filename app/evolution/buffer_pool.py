"""evolution/buffer_pool.py — 策略建议缓冲池

职责：
  1. 接收反思引擎产出的建议
  2. 按场景标签路由到 pending/ 或 clusters/
  3. 管理建议生命周期（过期清理）
"""
import json
import yaml
import shutil
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Any

from evolution.config import EvolutionConfig
from evolution.reflection_engine import ReflectionResult


class BufferPool:
    def __init__(self, cfg: EvolutionConfig):
        self.cfg = cfg
        self.root = Path(cfg.buffer.path)
        self.pending_dir = self.root / "pending"
        self.clusters_dir = self.root / "clusters"
        self.confirmed_dir = self.root / "confirmed"
        self.expired_dir = self.root / "expired"

    def ingest(self, result: ReflectionResult) -> str:
        """接收一条反思结果，写入缓冲池。"""
        sug = result.suggestion

        if sug.match_level in ("low", "strategy_gap"):
            return "skipped"

        data = self._result_to_dict(result)
        file_path = self.pending_dir / f"{result.suggestion_id}.yaml"
        with open(file_path, "w") as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False)

        return "buffered"

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

    def load_pending(self) -> List[Dict]:
        """加载所有 pending 建议。"""
        results = []
        for f in self.pending_dir.glob("*.yaml"):
            with open(f) as fh:
                data = yaml.safe_load(fh)
                data["_file"] = str(f)
                results.append(data)
        return results

    def load_cluster(self, cluster_id: str) -> Optional[Dict]:
        """加载指定 cluster 的全部建议。"""
        cluster_file = self.clusters_dir / f"{cluster_id}.yaml"
        if not cluster_file.exists():
            return None
        with open(cluster_file) as f:
            return yaml.safe_load(f)

    def list_clusters(self) -> List[str]:
        """列出所有 cluster ID。"""
        return [f.stem for f in self.clusters_dir.glob("*.yaml")]

    def move_to_confirmed(self, cluster_id: str):
        """将 cluster 移入 confirmed/。"""
        src = self.clusters_dir / f"{cluster_id}.yaml"
        dst = self.confirmed_dir / f"{cluster_id}.yaml"
        if src.exists():
            shutil.move(str(src), str(dst))

    def expire_old_suggestions(self) -> int:
        """清理过期建议，返回清理数量。"""
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.cfg.buffer.max_age_days)
        count = 0

        for f in self.pending_dir.glob("*.yaml"):
            with open(f) as fh:
                data = yaml.safe_load(fh)
            created = data.get("created_at", "")
            if created:
                try:
                    created_dt = datetime.fromisoformat(created)
                    if created_dt < cutoff:
                        shutil.move(str(f), str(self.expired_dir / f.name))
                        count += 1
                except (ValueError, TypeError):
                    pass
        return count

    def get_status(self) -> Dict:
        """返回缓冲池状态摘要。"""
        pending_count = len(list(self.pending_dir.glob("*.yaml")))
        cluster_count = len(list(self.clusters_dir.glob("*.yaml")))
        confirmed_count = len(list(self.confirmed_dir.glob("*.yaml")))
        expired_count = len(list(self.expired_dir.glob("*.yaml")))

        cluster_details = []
        for cf in self.clusters_dir.glob("*.yaml"):
            with open(cf) as f:
                data = yaml.safe_load(f)
            cluster_details.append({
                "cluster_id": cf.stem,
                "suggestion_count": len(data.get("suggestions", [])),
                "target_skill": data.get("target_skill", ""),
                "avg_causal_strength": data.get("avg_causal_strength", 0),
            })

        return {
            "pending_count": pending_count,
            "cluster_count": cluster_count,
            "confirmed_count": confirmed_count,
            "expired_count": expired_count,
            "clusters": cluster_details,
        }
