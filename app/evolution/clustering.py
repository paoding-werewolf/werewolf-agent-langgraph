"""evolution/clustering.py — 建议语义聚类

将 pending/ 中的建议按场景标签分组，然后在组内做语义一致性检查。
"""
import yaml
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime, timezone
from collections import Counter

from evolution.config import EvolutionConfig
from agents.llm_caller import LLMCaller


class SuggestionClusterer:
    def __init__(self, cfg: EvolutionConfig):
        self.cfg = cfg
        self.buffer_root = Path(cfg.buffer.path)
        self.clusters_dir = self.buffer_root / "clusters"
        self.pending_dir = self.buffer_root / "pending"

        self.cluster_llm = LLMCaller()
        if cfg.clustering_model:
            self.cluster_llm.model = cfg.clustering_model

    def process_pending(self) -> List[Dict]:
        """处理所有 pending 建议，归入或创建 cluster。"""
        processed = []
        pending_files = sorted(self.pending_dir.glob("*.yaml"))

        for pf in pending_files:
            with open(pf) as f:
                suggestion = yaml.safe_load(f)

            result = self._assign_to_cluster(suggestion)
            processed.append({
                "suggestion_id": suggestion.get("suggestion_id"),
                "action": result["action"],
                "cluster_id": result.get("cluster_id"),
            })

            pf.unlink()

        return processed

    def _assign_to_cluster(self, suggestion: Dict) -> Dict:
        """将一条建议分配到最匹配的 cluster，或创建新 cluster。"""
        scene_tags = suggestion.get("scene_tags", {})
        target_skill = suggestion.get("suggestion", {}).get("target_skill", "")

        best_match = None
        best_score = 0

        for cf in self.clusters_dir.glob("*.yaml"):
            with open(cf) as f:
                cluster = yaml.safe_load(f)

            score = self._scene_tag_overlap(scene_tags, cluster.get("scene_tags", {}))
            if score > best_score and score >= self.cfg.buffer.semantic_similarity_threshold:
                best_score = score
                best_match = cf.stem

        if best_match:
            cluster_file = self.clusters_dir / f"{best_match}.yaml"
            with open(cluster_file) as f:
                cluster = yaml.safe_load(f)

            if self._check_semantic_consistency(suggestion, cluster):
                cluster["suggestions"].append(suggestion)
                cluster["updated_at"] = datetime.now(timezone.utc).isoformat()
                cluster["avg_causal_strength"] = self._avg_causal_strength(cluster["suggestions"])
                cluster["consistency_rate"] = self._consistency_rate(cluster["suggestions"])

                if len(cluster["suggestions"]) > self.cfg.buffer.max_cluster_size:
                    cluster["suggestions"].sort(key=lambda s: s.get("created_at", ""))
                    cluster["suggestions"] = cluster["suggestions"][-self.cfg.buffer.max_cluster_size:]

                with open(cluster_file, "w") as f:
                    yaml.dump(cluster, f, allow_unicode=True, default_flow_style=False)

                return {"action": "added_to_cluster", "cluster_id": best_match}
            else:
                return self._create_cluster(suggestion, scene_tags, target_skill)
        else:
            return self._create_cluster(suggestion, scene_tags, target_skill)

    def _create_cluster(self, suggestion: Dict, scene_tags: Dict, target_skill: str) -> Dict:
        """创建新 cluster。"""
        cluster_id = f"cluster_{target_skill}_{self._scene_tag_key(scene_tags)}"
        cluster_file = self.clusters_dir / f"{cluster_id}.yaml"

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

        with open(cluster_file, "w") as f:
            yaml.dump(cluster, f, allow_unicode=True, default_flow_style=False)

        return {"action": "new_cluster", "cluster_id": cluster_id}

    def _scene_tag_overlap(self, tags_a: Dict, tags_b: Dict) -> float:
        """计算两组场景标签的相似度（0-1）。"""
        key_fields = ["role", "role_survived_rounds", "critical_phase",
                       "wolf_aggression", "result", "sheriff_contested",
                       "first_night_target"]
        match_count = 0
        total = len(key_fields)

        for field_name in key_fields:
            val_a = tags_a.get(field_name)
            val_b = tags_b.get(field_name)
            if val_a is not None and val_b is not None:
                if str(val_a) == str(val_b):
                    match_count += 1
                elif field_name == "role_survived_rounds":
                    try:
                        if abs(int(val_a) - int(val_b)) <= 1:
                            match_count += 0.5
                    except (ValueError, TypeError):
                        pass

        return match_count / total if total > 0 else 0

    def _check_semantic_consistency(self, new_suggestion: Dict, cluster: Dict) -> bool:
        """用 LLM 判断新建议与 cluster 内已有建议是否语义一致。"""
        if not cluster.get("suggestions"):
            return True

        new_text = new_suggestion.get("suggestion", {}).get("text", "")
        new_direction = new_suggestion.get("suggestion", {}).get("direction", "")

        existing_directions = [
            s.get("suggestion", {}).get("direction", "")
            for s in cluster["suggestions"]
        ]
        if new_direction == "discard" and "modify" in existing_directions:
            return False
        if new_direction == "modify" and all(d == "discard" for d in existing_directions):
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
                max_tokens=20,
            )
            answer = (resp.choices[0].message.content or "").strip().lower()
            return "consistent" in answer
        except Exception:
            return True

    def _avg_causal_strength(self, suggestions: List[Dict]) -> float:
        strengths = [s.get("suggestion", {}).get("causal_strength", 0) for s in suggestions]
        return sum(strengths) / len(strengths) if strengths else 0

    def _consistency_rate(self, suggestions: List[Dict]) -> float:
        """计算 cluster 内建议方向的一致率。"""
        if not suggestions:
            return 0
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
