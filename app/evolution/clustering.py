"""evolution/clustering.py — 建议语义聚类（MySQL 持久化）

将 pending 中的建议按场景标签分组，然后在组内做语义一致性检查。
"""
import logging
from typing import List, Dict

from evolution.config import EvolutionConfig
from evolution.buffer_pool import BufferPool
from agents.llm_caller import LLMCaller

logger = logging.getLogger("evolution.clustering")


class SuggestionClusterer:
    def __init__(self, cfg: EvolutionConfig, pool: BufferPool):
        self.cfg = cfg
        self.pool = pool

        self.cluster_llm = LLMCaller()
        if cfg.clustering_model:
            self.cluster_llm.model = cfg.clustering_model

    def process_pending(self) -> List[Dict]:
        """处理所有 pending 建议，归入或创建 cluster。"""
        processed = []
        pending = self.pool.load_pending()
        logger.info(f"Processing {len(pending)} pending suggestions")

        for suggestion in pending:
            result = self._assign_to_cluster(suggestion)
            processed.append({
                "suggestion_id": suggestion.get("suggestion_id"),
                "action": result["action"],
                "cluster_id": result.get("cluster_id"),
            })
            logger.info(f"Suggestion {suggestion.get('suggestion_id', '?')}: {result['action']} -> {result.get('cluster_id', 'N/A')}")
            self.pool.delete_pending(suggestion.get("_item_key") or suggestion.get("suggestion_id", ""))

        return processed

    def _assign_to_cluster(self, suggestion: Dict) -> Dict:
        """将一条建议分配到最匹配的 cluster，或创建新 cluster。"""
        scene_tags = suggestion.get("scene_tags", {})
        target_skill = suggestion.get("suggestion", {}).get("target_skill", "")

        best_match = None
        best_score = 0

        cluster_ids = self.pool.list_clusters()
        for cluster_id in cluster_ids:
            cluster = self.pool.load_cluster(cluster_id)
            if not cluster:
                continue
            score = self._scene_tag_overlap(scene_tags, cluster.get("scene_tags", {}))
            if score > best_score and score >= self.cfg.buffer.semantic_similarity_threshold:
                best_score = score
                best_match = cluster_id

        if best_match:
            cluster = self.pool.load_cluster(best_match)
            if cluster and self._check_semantic_consistency(suggestion, cluster, best_score):
                cluster["suggestions"].append(suggestion)
                from datetime import datetime, timezone
                cluster["updated_at"] = datetime.now(timezone.utc).isoformat()
                cluster["avg_causal_strength"] = self._avg_causal_strength(cluster["suggestions"])
                cluster["consistency_rate"] = self._consistency_rate(cluster["suggestions"])
                # Ensure target_skill is set from suggestions if column was empty
                if not cluster.get("target_skill") and target_skill:
                    cluster["target_skill"] = target_skill

                if len(cluster["suggestions"]) > self.cfg.buffer.max_cluster_size:
                    cluster["suggestions"].sort(key=lambda s: s.get("created_at", ""))
                    cluster["suggestions"] = cluster["suggestions"][-self.cfg.buffer.max_cluster_size:]

                self.pool.save_cluster(best_match, cluster)
                logger.info(f"Added to existing cluster: {best_match} (score={best_score:.2f}, total_suggestions={len(cluster['suggestions'])})")
                return {"action": "added_to_cluster", "cluster_id": best_match}
            else:
                logger.info(f"Scene matched cluster {best_match} (score={best_score:.2f}) but semantic check failed or cluster missing, creating new cluster")
                return self._create_cluster(suggestion, scene_tags, target_skill)
        else:
            logger.info(f"No matching cluster found for target={target_skill} (best_score={best_score:.2f}, threshold={self.cfg.buffer.semantic_similarity_threshold}), creating new cluster")
            return self._create_cluster(suggestion, scene_tags, target_skill)

    def _create_cluster(self, suggestion: Dict, scene_tags: Dict, target_skill: str) -> Dict:
        """创建新 cluster。"""
        import uuid
        from datetime import datetime, timezone

        cluster_id = f"cluster_{target_skill}_{self._scene_tag_key(scene_tags)}_{uuid.uuid4().hex[:6]}"

        cluster = {
            "cluster_id": cluster_id,
            "target_skill": target_skill,
            "scene_tags": scene_tags,
            "suggestions": [suggestion],
            "avg_causal_strength": suggestion.get("suggestion", {}).get("causal_strength", 0),
            "consistency_rate": 1.0,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        self.pool.save_cluster(cluster_id, cluster)
        return {"action": "new_cluster", "cluster_id": cluster_id}

    def _scene_tag_overlap(self, tags_a: Dict, tags_b: Dict) -> float:
        """计算两组场景标签的相似度（0-1），核心维度加权优先。

        核心维度（role, critical_phase, result）各占 0.25，合计 0.75；
        次要维度（其余 4 个）各占 0.0625，合计 0.25。
        同 role + 同 phase + 同 result 即可达到 0.75，轻松过阈值。
        """
        core_fields = ["role", "critical_phase", "result"]
        secondary_fields = ["role_survived_rounds", "wolf_aggression",
                            "sheriff_contested", "first_night_target"]

        score = 0.0

        # 核心维度：精确匹配各 0.25
        for field_name in core_fields:
            val_a = tags_a.get(field_name)
            val_b = tags_b.get(field_name)
            if val_a is not None and val_b is not None and str(val_a) == str(val_b):
                score += 0.25

        # 次要维度：精确匹配各 0.0625，数值近邻半分
        for field_name in secondary_fields:
            val_a = tags_a.get(field_name)
            val_b = tags_b.get(field_name)
            if val_a is not None and val_b is not None:
                if str(val_a) == str(val_b):
                    score += 0.0625
                elif field_name == "role_survived_rounds":
                    try:
                        if abs(int(val_a) - int(val_b)) <= 1:
                            score += 0.03125
                    except (ValueError, TypeError):
                        pass

        return min(score, 1.0)

    def _check_semantic_consistency(self, new_suggestion: Dict, cluster: Dict,
                                        overlap_score: float = 0.0) -> bool:
        """用 LLM 判断新建议与 cluster 内已有建议是否语义一致。

        overlap_score >= 0.75 时（核心维度全部匹配），跳过 LLM 调用直接通过。
        """
        if not cluster.get("suggestions"):
            return True

        if overlap_score >= 0.75:
            logger.info(f"Overlap score {overlap_score:.2f} >= 0.75, skipping semantic LLM check (auto-consistent)")
            return True

        new_text = new_suggestion.get("suggestion", {}).get("text", "")
        new_direction = new_suggestion.get("suggestion", {}).get("direction", "")

        existing_directions = [
            s.get("suggestion", {}).get("direction", "")
            for s in cluster["suggestions"]
        ]
        if new_direction == "discard" and "modify" in existing_directions:
            logger.info(f"Semantic check: direction conflict (discard vs modify), returning inconsistent")
            return False
        if new_direction == "modify" and all(d == "discard" for d in existing_directions):
            logger.info(f"Semantic check: direction conflict (modify vs all-discard), returning inconsistent")
            return False

        existing_texts = [
            s.get("suggestion", {}).get("text", "")
            for s in cluster["suggestions"][-3:]
        ]

        prompt = f"""判断以下策略建议在语义上是否一致（在说同一件事或同一方向）：

新建议："{new_text}"

已有建议：
{chr(10).join(f'- "{t}"' for t in existing_texts)}

只回答 "consistent" 或 "inconsistent"，不要其他内容。"""

        try:
            resp = self.cluster_llm.client.chat.completions.create(
                model=self.cluster_llm.model,
                messages=[
                    {"role": "system", "content": "You are a semantic similarity judge."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                max_tokens=200,
            )
            answer = (resp.choices[0].message.content or "").strip().lower()
            if not answer:
                logger.warning("Semantic LLM check returned empty content, defaulting to consistent (allow merge)")
                return True
            result = "consistent" in answer
            logger.info(f"Semantic LLM check: answer={answer!r}, result={'consistent' if result else 'inconsistent'}")
            return result
        except Exception as e:
            # LLM 失败时默认允许归入 cluster，避免碎片化
            logger.warning(f"Semantic LLM check failed: {e}, defaulting to consistent (allow merge)")
            return True

    def _avg_causal_strength(self, suggestions: List[Dict]) -> float:
        strengths = [s.get("suggestion", {}).get("causal_strength", 0) for s in suggestions]
        return sum(strengths) / len(strengths) if strengths else 0

    def _consistency_rate(self, suggestions: List[Dict]) -> float:
        """计算 cluster 内建议方向的一致率。"""
        if not suggestions:
            return 0
        from collections import Counter
        directions = [s.get("suggestion", {}).get("direction", "") for s in suggestions]
        counts = Counter(directions)
        most_common_count = counts.most_common(1)[0][1]
        return most_common_count / len(directions)

    def _scene_tag_key(self, tags: Dict) -> str:
        """将场景标签压缩为短字符串，用作 cluster ID 后缀。"""
        parts = []
        if tags.get("role"):
            parts.append(tags["role"])
        if tags.get("critical_phase"):
            parts.append(tags["critical_phase"].replace("_", "-"))
        if tags.get("result"):
            parts.append(tags["result"])
        return "_".join(parts) if parts else "unknown"
