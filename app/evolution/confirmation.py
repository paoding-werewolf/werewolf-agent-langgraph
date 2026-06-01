"""evolution/confirmation.py — 双重确认判定

判定一个 cluster 是否满足条件执行策略更新：
  维度一：频率阈值（cluster 内建议数 ≥ N_min 且一致率 ≥ R_min）
  维度二：因果强度阈值（平均 causal_strength ≥ C_min）

特殊通道：高因果强度快速确认（causal_strength ≥ 0.8 → 只需 2 次）
"""
import yaml
from pathlib import Path
from typing import Dict, Optional
from datetime import datetime, timezone

from evolution.config import EvolutionConfig
from evolution.buffer_pool import BufferPool
from evolution.version_manager import VersionManager


class ConfirmationJudge:
    def __init__(self, cfg: EvolutionConfig, buffer_pool: BufferPool,
                 version_manager: VersionManager):
        self.cfg = cfg
        self.buffer_pool = buffer_pool
        self.version_manager = version_manager

    def check_all_clusters(self) -> list:
        """遍历所有 cluster，执行确认判定。"""
        confirmed = []
        for cluster_id in self.buffer_pool.list_clusters():
            cluster = self.buffer_pool.load_cluster(cluster_id)
            if not cluster:
                continue

            result = self.judge(cluster)
            if result["confirmed"]:
                confirmed.append({
                    "cluster_id": cluster_id,
                    **result,
                })
                self._execute_confirmation(cluster, cluster_id)

        return confirmed

    def judge(self, cluster: Dict) -> Dict:
        """对单个 cluster 执行双重判定。"""
        suggestions = cluster.get("suggestions", [])
        count = len(suggestions)
        consistency = cluster.get("consistency_rate", 0)
        avg_causal = cluster.get("avg_causal_strength", 0)

        cfm = self.cfg.confirmation

        # 快速通道：高因果强度 + 方向一致
        if avg_causal >= cfm.fast_track_min_causal_strength:
            if count >= cfm.fast_track_min_count and consistency >= cfm.normal_min_consistency_rate:
                return {
                    "confirmed": True,
                    "reason": (
                        f"Fast track: causal={avg_causal:.2f} >= {cfm.fast_track_min_causal_strength}, "
                        f"count={count} >= {cfm.fast_track_min_count}, "
                        f"consistency={consistency:.2f} >= {cfm.normal_min_consistency_rate}"
                    ),
                    "count": count,
                    "consistency_rate": consistency,
                    "avg_causal_strength": avg_causal,
                    "fast_track": True,
                }
            else:
                reasons = []
                if count < cfm.fast_track_min_count:
                    reasons.append(f"count={count}<{cfm.fast_track_min_count}")
                if consistency < cfm.normal_min_consistency_rate:
                    reasons.append(f"consistency={consistency:.2f}<{cfm.normal_min_consistency_rate}")
                return {
                    "confirmed": False,
                    "reason": f"Fast track eligible but {', '.join(reasons)}",
                    "count": count,
                    "consistency_rate": consistency,
                    "avg_causal_strength": avg_causal,
                    "fast_track": True,
                }

        # 普通通道：双重判定
        freq_ok = count >= cfm.normal_min_count and consistency >= cfm.normal_min_consistency_rate
        causal_ok = avg_causal >= cfm.normal_min_avg_causal_strength

        if freq_ok and causal_ok:
            return {
                "confirmed": True,
                "reason": f"Normal: count={count}>={cfm.normal_min_count}, consistency={consistency:.2f}>={cfm.normal_min_consistency_rate}, causal={avg_causal:.2f}>={cfm.normal_min_avg_causal_strength}",
                "count": count,
                "consistency_rate": consistency,
                "avg_causal_strength": avg_causal,
                "fast_track": False,
            }

        reasons = []
        if not freq_ok:
            if count < cfm.normal_min_count:
                reasons.append(f"count={count}<{cfm.normal_min_count}")
            if consistency < cfm.normal_min_consistency_rate:
                reasons.append(f"consistency={consistency:.2f}<{cfm.normal_min_consistency_rate}")
        if not causal_ok:
            reasons.append(f"causal={avg_causal:.2f}<{cfm.normal_min_avg_causal_strength}")

        return {
            "confirmed": False,
            "reason": f"Not confirmed: {', '.join(reasons)}",
            "count": count,
            "consistency_rate": consistency,
            "avg_causal_strength": avg_causal,
            "fast_track": False,
        }

    def _execute_confirmation(self, cluster: Dict, cluster_id: str):
        """确认执行：将 cluster 的建议合成为策略新版本。"""
        target_skill = cluster.get("target_skill", "")
        suggestions = cluster.get("suggestions", [])

        if not target_skill or not suggestions:
            return

        new_content = self._synthesize_strategy(suggestions, target_skill)

        self.version_manager.create_new_version(
            skill_name=target_skill,
            content=new_content,
            source="debounced_update",
            trigger_cluster=cluster_id,
        )

        self.buffer_pool.move_to_confirmed(cluster_id)

    def _synthesize_strategy(self, suggestions: list, target_skill: str) -> str:
        """将多条建议合成为一份完整的策略文档。"""
        from agents.llm_caller import llm

        suggestions_text = "\n".join(
            f"- 建议 {i+1}: {s.get('suggestion', {}).get('text', '')}\n"
            f"  因果强度: {s.get('suggestion', {}).get('causal_strength', 0)}\n"
            f"  因果链: {self._format_causal_chain(s.get('causal_chain', []))}"
            for i, s in enumerate(suggestions)
        )

        current_content = self.version_manager.load_skill_full(target_skill) or ""

        prompt = f"""基于以下多条对局反思建议，生成一份更新后的策略文档。

目标策略：{target_skill}
当前策略内容：
{current_content or "（尚无现有策略）"}

来自 {len(suggestions)} 局对局的建议：
{suggestions_text}

要求：
1. 输出完整的策略 Markdown 文件（含 YAML frontmatter）
2. 保留当前策略中仍然有效的部分
3. 根据建议修改或新增策略条目
4. 格式遵循：
   ---
   name: {target_skill}
   description: <一句话描述>
   version: <下一个版本号>
   role: <角色>
   tags: [<标签>]
   source: debounced_update
   ---
   ## When to Use
   ## Procedure
   ## Pitfalls
   ## Verification
"""

        try:
            resp = llm.client.chat.completions.create(
                model=llm.model,
                messages=[
                    {"role": "system", "content": "你是狼人杀策略文档编写专家。输出完整的 Markdown 文件。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
            )
            return resp.choices[0].message.content or ""
        except Exception:
            return self._fallback_synthesize(suggestions, target_skill)

    def _format_causal_chain(self, chain: list) -> str:
        parts = []
        for step in chain:
            parts.append(f"{step.get('action', '')} -> {step.get('intermediate', '')} -> {step.get('outcome', '')}")
        return " | ".join(parts)

    def _fallback_synthesize(self, suggestions: list, target_skill: str) -> str:
        """LLM 不可用时的降级合成。"""
        lines = [
            "---",
            f"name: {target_skill}",
            f"description: 基于 {len(suggestions)} 局对局反思的策略",
            "source: debounced_update",
            "---",
            "",
            "## When to Use",
            "当相关场景出现时参考此策略。",
            "",
            "## Procedure",
        ]
        for i, s in enumerate(suggestions):
            text = s.get("suggestion", {}).get("text", "")
            if text:
                lines.append(f"{i+1}. {text}")

        lines.extend([
            "",
            "## Pitfalls",
            "- 以上策略基于有限对局数据，需要根据实际情况灵活调整",
            "",
            "## Verification",
            "- 使用后记录胜率变化",
        ])
        return "\n".join(lines)
