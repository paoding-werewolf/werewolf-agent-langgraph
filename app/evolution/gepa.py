"""evolution/gepa.py — GEPA（Genetic-Pareto Prompt Evolution）离线进化模块

利用遗传算法 + LLM 对策略文档进行多目标批量优化：
  1. 触发 — 检查前置条件（最少策略数、最少对局数据）
  2. 初始化种群 — 加载当前策略版本作为初始种群
  3. 适应度评估 — 多维度打分（胜率 + LLM-as-Judge 评估策略质量）
  4. Pareto 前沿选择 — 跨多个适应度维度筛选非支配策略
  5. LLM 诊断 — 对表现不佳的策略，从对局 trace 中诊断失败模式
  6. LLM 语义变异 — 基于诊断，LLM 生成有意义的策略修改
  7. 系统感知交叉 — 混合不同策略的成功部分
  8. 创建新版本 — 变异/交叉策略以 source="gepa_evolution" 入库
  9. 状态持久化 — 每代保存进度到 evolution_runtime_state (key="gepa")
"""
import asyncio
import json
import logging
import random
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from agents.llm_caller import LLMCaller
from evolution.config import EvolutionConfig, GEPAConfig
from evolution.db import get_session
from evolution.models import (
    EvolutionGameArchive,
    EvolutionRuntimeState,
    EvolutionSkill,
    EvolutionSkillVersion,
)
from evolution.skill_loader import SkillLoader
from evolution.version_manager import VersionManager

logger = logging.getLogger("gepa")

# ── 适应度维度 ─────────────────────────────────────────────────
FITNESS_DIMENSIONS = ["win_rate", "consistency", "deception", "info_utilization"]


# ── GEPA 主类 ──────────────────────────────────────────────────

GEPA_STEPS = [
    ("initializing", "初始化种群"),
    ("evaluating_fitness", "适应度评估"),
    ("pareto_selection", "Pareto 前沿选择"),
    ("mutating", "LLM 诊断+变异"),
    ("crossing_over", "LLM 交叉"),
    ("creating_versions", "创建新版本"),
    ("saving_state", "保存状态"),
]


class GEPA:
    """Genetic-Pareto Prompt Evolution 离线进化引擎。"""

    def __init__(self, cfg: EvolutionConfig):
        self.cfg = cfg
        self.gepa_cfg: GEPAConfig = cfg.gepa
        self.loader = SkillLoader(cfg)
        self.vm = VersionManager(cfg)
        self._cancel_flag = False

    # ── 对外 API ──────────────────────────────────────────────

    def trigger(self, cfg: EvolutionConfig) -> Dict[str, Any]:
        """触发 GEPA 运行。返回 {"status": "started"} 或错误信息。"""
        state = self._load_state()
        if state.get("status") == "running":
            return {"status": "error", "detail": "GEPA 已经在运行中"}

        # 前置条件检查
        prereq = self._check_prerequisites()
        if not prereq["ok"]:
            return {"status": "error", "detail": prereq["reason"]}

        # 初始化状态（保留上次运行的结果，直到新运行完成后再覆盖）
        now = datetime.now(timezone.utc).isoformat()
        prev_state = self._load_state()
        initial_state = {
            "status": "running",
            "current_generation": 0,
            "current_step": "initializing",
            "total_generations": self.gepa_cfg.num_generations,
            "population_size": self.gepa_cfg.population_size,
            "started_at": now,
            "updated_at": now,
            "completed_at": None,
            "latest_results": (prev_state or {}).get("latest_results", {}),
            "history": (prev_state or {}).get("history", []),
        }
        self._save_state(initial_state)
        self._cancel_flag = False

        # 在后台启动 run()
        asyncio.create_task(self._run_wrapper(cfg))
        return {"status": "started"}

    def get_status(self) -> Dict[str, Any]:
        """加载 GEPA 当前状态。"""
        state = self._load_state()
        if not state:
            return {
                "status": "idle",
                "current_generation": 0,
                "current_step": None,
                "total_generations": self.gepa_cfg.num_generations,
                "population_size": self.gepa_cfg.population_size,
                "started_at": None,
                "updated_at": None,
                "completed_at": None,
                "latest_results": {},
                "history": [],
            }
        return state

    def cancel(self) -> Dict[str, Any]:
        """标记 GEPA 为已取消。"""
        self._cancel_flag = True
        state = self._load_state()
        if state.get("status") == "running":
            state["status"] = "cancelled"
            state["completed_at"] = datetime.now(timezone.utc).isoformat()
            state["updated_at"] = datetime.now(timezone.utc).isoformat()
            self._save_state(state)
            return {"status": "cancelled"}
        return {"status": "idle", "detail": "GEPA 未在运行"}

    async def _run_wrapper(self, cfg: EvolutionConfig):
        """异步包装器，在线程池中运行同步的 run()，避免阻塞事件循环。"""
        try:
            await asyncio.to_thread(self.run, cfg)
        except Exception:
            logger.exception("GEPA run() 异常终止")
            state = self._load_state()
            if state.get("status") == "running":
                state["status"] = "error"
                state["completed_at"] = datetime.now(timezone.utc).isoformat()
                state["updated_at"] = datetime.now(timezone.utc).isoformat()
                state["latest_results"]["error"] = traceback.format_exc()
                self._save_state(state)

    # ── 主循环 ────────────────────────────────────────────────

    def run(self, cfg: EvolutionConfig):
        """GEPA 主管道（同步，可被 asyncio.create_task 包装为后台任务）。

        每代：
          1. 加载种群（首次从 DB 加载，后续包含变异/交叉个体）
          2. 评估适应度
          3. Pareto 前沿选择
          4. LLM 诊断 + 变异
          5. 交叉
          6. 创建新版本
          7. 保存状态
        """
        gepa_cfg = cfg.gepa
        self._cancel_flag = False

        # ── 初始化种群 ────────────────────────────────────────
        self._update_step("initializing")
        population = self._initialize_population()
        if not population:
            logger.warning("GEPA: 种群为空，退出")
            self._mark_completed("completed", latest_results={"error": "种群为空"})
            return

        # 补齐种群到 population_size
        while len(population) < gepa_cfg.population_size:
            parent = random.choice(population)
            population.append(self._clone_individual(parent))

        # 截断到 population_size
        population = population[:gepa_cfg.population_size]

        logger.info(
            f"GEPA: 启动进化，种群={len(population)}，代数={gepa_cfg.num_generations}"
        )

        for gen in range(1, gepa_cfg.num_generations + 1):
            if self._cancel_flag:
                logger.info(f"GEPA: 第 {gen} 代被取消")
                break

            logger.info(f"GEPA: 开始第 {gen}/{gepa_cfg.num_generations} 代")

            # ── 评估适应度 ─────────────────────────────────────
            self._update_step("evaluating_fitness", gen)
            fitness_results = self._evaluate_fitness(population)

            # ── Pareto 前沿选择 ────────────────────────────────
            self._update_step("pareto_selection", gen)
            pareto_front = self._pareto_front(fitness_results)

            # ── LLM 诊断 + 变异 ───────────────────────────────
            self._update_step("mutating", gen)
            mutations = 0
            new_individuals = []
            for ind in population:
                if self._cancel_flag:
                    break
                fitness = self._get_individual_fitness(ind, fitness_results)
                # 对非 Pareto 前沿的个体进行变异
                if not self._is_in_pareto_front(ind, pareto_front):
                    mutated = self._llm_diagnose_and_mutate(ind, fitness)
                    if mutated:
                        new_individuals.append(mutated)
                        mutations += 1

            # ── 交叉 ──────────────────────────────────────────
            self._update_step("crossing_over", gen)
            crossovers = 0
            if len(pareto_front) >= 2:
                # 在 Pareto 前沿个体之间做交叉
                pareto_individuals = [
                    self._find_individual_by_key(k, population)
                    for k in pareto_front
                ]
                for i in range(0, len(pareto_individuals) - 1, 2):
                    if self._cancel_flag:
                        break
                    child = self._llm_crossover(
                        pareto_individuals[i], pareto_individuals[i + 1]
                    )
                    if child:
                        new_individuals.append(child)
                        crossovers += 1

            # ── 创建新版本（变异/交叉策略入库）──────────────────
            self._update_step("creating_versions", gen)
            new_versions = []
            for ind in new_individuals:
                version_name = self.loader.create_new_version(
                    skill_name=ind["skill_name"],
                    content=ind["content"],
                    source="gepa_evolution",
                    trigger_cluster=f"gepa_g{gen}",
                )
                new_versions.append(f"{ind['skill_name']}:{version_name}")
                ind["version"] = version_name
                ind["source"] = "gepa_evolution"

            # ── 更新种群：保留 Pareto 前沿 + 新个体 ─────────────
            # 先保留 Pareto 前沿个体
            next_population = []
            for key in pareto_front:
                found = self._find_individual_by_key(key, population)
                if found:
                    next_population.append(found)

            # 补充新个体
            next_population.extend(new_individuals)

            # 如果种群不够，从上一代随机补充
            while len(next_population) < gepa_cfg.population_size:
                parent = random.choice(population)
                next_population.append(self._clone_individual(parent))

            # 截断
            next_population = next_population[:gepa_cfg.population_size]
            population = next_population

            # ── 计算最佳适应度 ─────────────────────────────────
            best_fitness = {}
            if fitness_results:
                best_per_dim = {}
                for key, scores in fitness_results.items():
                    for dim, val in scores.items():
                        if dim not in best_per_dim or val > best_per_dim[dim]:
                            best_per_dim[dim] = val
                best_fitness = best_per_dim

            # ── 记录代际摘要 ───────────────────────────────────
            gen_summary = {
                "generation": gen,
                "population_size": len(population),
                "pareto_front_size": len(pareto_front),
                "pareto_front": pareto_front,
                "new_versions_created": new_versions,
                "mutations": mutations,
                "crossovers": crossovers,
                "best_fitness": best_fitness,
            }

            # ── 持久化状态 ─────────────────────────────────────
            self._update_step("saving_state", gen)
            latest_results = {
                "generation": gen,
                "best_fitness": best_fitness,
                "pareto_front": pareto_front,
                "new_versions_created": new_versions,
                "mutations": mutations,
                "crossovers": crossovers,
            }

            state = self._load_state()
            state["current_generation"] = gen
            state["updated_at"] = datetime.now(timezone.utc).isoformat()
            state["latest_results"] = latest_results
            state["history"].append(gen_summary)
            self._save_state(state)

            logger.info(
                f"GEPA: 第 {gen} 代完成 — 变异={mutations}, 交叉={crossovers}, "
                f"Pareto前沿={len(pareto_front)}, 新版本={len(new_versions)}"
            )

        # ── 进化完成 ──────────────────────────────────────────
        final_status = "cancelled" if self._cancel_flag else "completed"
        self._mark_completed(final_status)
        logger.info(f"GEPA: 进化完成，状态={final_status}")

    # ── 前置条件检查 ──────────────────────────────────────────

    def _check_prerequisites(self) -> Dict[str, Any]:
        """检查 GEPA 运行的前置条件。"""
        if not self.gepa_cfg.enabled:
            return {"ok": False, "reason": "GEPA 未启用 (gepa.enabled=false)"}

        session = get_session()
        try:
            skill_count = session.query(EvolutionSkill).count()
            if skill_count < self.gepa_cfg.min_skills_in_library:
                return {
                    "ok": False,
                    "reason": (
                        f"策略库不足: 有 {skill_count} 个策略，"
                        f"至少需要 {self.gepa_cfg.min_skills_in_library}"
                    ),
                }

            # 检查是否有足够的对局数据（策略级累计，不受版本更新重置影响）
            skills_with_enough_games = session.query(EvolutionSkill).filter(
                EvolutionSkill.skill_games_played >= self.gepa_cfg.min_games_for_fitness
            ).count()
            if skills_with_enough_games < 1:
                return {
                    "ok": False,
                    "reason": (
                        f"对局数据不足: 没有策略达到 "
                        f"{self.gepa_cfg.min_games_for_fitness} 场对局的最低要求（策略级累计）"
                    ),
                }

            return {"ok": True, "reason": ""}
        finally:
            session.close()

    # ── 种群初始化 ────────────────────────────────────────────

    def _initialize_population(self) -> List[Dict[str, Any]]:
        """从数据库加载当前策略版本作为初始种群。

        每个个体包含:
          - key: "{skill_name}:{version}" 唯一标识
          - skill_name: 策略名
          - skill_id: 策略 ID
          - version: 版本号
          - content: Markdown 内容
          - games_played, wins, win_rate: 历史数据
          - source: 来源
        """
        population = []
        session = get_session()
        try:
            skills = session.query(EvolutionSkill).all()
            for skill in skills:
                versions = session.query(EvolutionSkillVersion).filter_by(
                    skill_id=skill.id
                ).all()
                for v in versions:
                    # 跳过 archived 版本
                    if v.status == "archived":
                        continue
                    ind = {
                        "key": f"{skill.skill_name}:{v.version}",
                        "skill_name": skill.skill_name,
                        "skill_id": skill.id,
                        "version": v.version,
                        "content": v.content_markdown,
                        "games_played": v.games_played,
                        "wins": v.wins,
                        "win_rate": float(v.win_rate or 0.0),
                        "skill_games_played": skill.skill_games_played,
                        "skill_win_rate": float(skill.skill_win_rate or 0.0),
                        "source": v.source,
                        "role": skill.role,
                        "status": v.status,
                    }
                    population.append(ind)
        finally:
            session.close()

        return population

    # ── 适应度评估 ────────────────────────────────────────────

    def _evaluate_fitness(self, population: List[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
        """多维度适应度评估。

        四个维度:
          - win_rate: 历史胜率（数据不足则惩罚）
          - consistency: 策略一致性（LLM 评估策略文档内部逻辑是否一致）
          - deception: 欺骗质量（狼人相关角色的伪装能力，LLM 评估）
          - info_utilization: 信息利用效率（LLM 评估策略对已知信息的利用程度）

        返回: {individual_key: {dim: score, ...}, ...}
        """
        results = {}

        # 准备 LLM
        judge_llm = LLMCaller()
        judge_llm.model = self.gepa_cfg.judge_model

        # 批量加载对局 trace（用于 LLM 评估上下文）
        game_traces = self._load_game_traces()

        for ind in population:
            scores = {}

            # ── 维度 1: 胜率 ───────────────────────────────
            base_wr = ind.get("win_rate", 0.0)
            games = ind.get("games_played", 0)
            skill_games = ind.get("skill_games_played", 0)
            skill_wr = ind.get("skill_win_rate", 0.0)
            min_games = self.gepa_cfg.min_games_for_fitness

            if games >= min_games:
                # 版本自身数据充足，直接用版本胜率
                scores["win_rate"] = base_wr
            elif skill_games >= min_games:
                # 版本数据不足但策略级数据充足，用策略级胜率作为先验，版本数据做微调
                version_ratio = games / min_games if min_games > 0 else 0
                scores["win_rate"] = base_wr * version_ratio + skill_wr * (1 - version_ratio)
            elif games > 0:
                # 都不足，线性惩罚
                penalty_ratio = games / min_games
                scores["win_rate"] = base_wr * penalty_ratio + 0.1 * (1 - penalty_ratio)
            else:
                scores["win_rate"] = 0.05  # 无数据最低分

            # ── 维度 2-4: LLM-as-Judge ──────────────────────
            role = ind.get("role", "common")
            content = ind.get("content", "")

            # 为该策略找相关的对局 trace
            related_traces = self._find_related_traces(
                ind["skill_name"], role, game_traces
            )

            judge_scores = self._llm_judge_evaluate(
                content, role, related_traces, judge_llm
            )
            scores["consistency"] = judge_scores.get("consistency", 0.3)
            scores["deception"] = judge_scores.get("deception", 0.3)
            scores["info_utilization"] = judge_scores.get("info_utilization", 0.3)

            results[ind["key"]] = scores

        return results

    def _load_game_traces(self) -> List[Dict[str, Any]]:
        """从 EvolutionGameArchive 加载近期对局记录。"""
        session = get_session()
        try:
            archives = session.query(EvolutionGameArchive).order_by(
                EvolutionGameArchive.created_at.desc()
            ).limit(50).all()

            traces = []
            for a in archives:
                payload = a.payload_json or {}
                trace_text = payload.get("full_trace", "")
                reflection = payload.get("reflection_report", "")
                strategies_used = payload.get("strategies_used", [])
                scene_tags = payload.get("scene_tags", {})

                traces.append({
                    "game_id": a.game_id,
                    "my_role": a.my_role or "",
                    "result": a.result or "",
                    "day_count": a.day_count,
                    "has_builtin_ai": a.has_builtin_ai,
                    "full_trace": trace_text[:3000] if trace_text else "",
                    "reflection_report": reflection[:1500] if reflection else "",
                    "strategies_used": strategies_used,
                    "scene_tags": scene_tags,
                })
            return traces
        finally:
            session.close()

    def _find_related_traces(
        self, skill_name: str, role: str, game_traces: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """找到与指定策略相关的对局 trace。"""
        related = []
        for t in game_traces:
            # 按角色匹配
            if role != "common" and t.get("my_role") != role:
                continue
            # 按策略名匹配
            used = t.get("strategies_used", [])
            if isinstance(used, list) and skill_name in used:
                related.append(t)
                continue
            # 如果没有直接匹配，角色相同的也纳入
            if role != "common" and t.get("my_role") == role:
                related.append(t)

        return related[:10]  # 最多取 10 条

    def _llm_judge_evaluate(
        self,
        content: str,
        role: str,
        related_traces: List[Dict[str, Any]],
        judge_llm: LLMCaller,
    ) -> Dict[str, float]:
        """LLM-as-Judge 评估策略文档质量。返回三维评分。"""
        # 构建对局 trace 摘要
        trace_summary = ""
        if related_traces:
            parts = []
            for t in related_traces[:5]:
                result_str = t.get("result", "unknown")
                trace_text = t.get("full_trace", "")[:500]
                parts.append(f"- 游戏 {t['game_id']}: 结果={result_str}, trace={trace_text}")
            trace_summary = "\n".join(parts)
        else:
            trace_summary = "（无相关对局记录）"

        prompt = f"""评估以下狼人杀策略文档的质量，给出三个维度的评分（0-1 浮点数）。

策略角色: {role}
策略内容:
{content[:3000]}

相关对局记录:
{trace_summary}

评分维度:
1. consistency (策略一致性): 策略文档内部逻辑是否自洽，各阶段的建议是否互相矛盾。0=严重矛盾，1=完全一致。
2. deception (欺骗质量): 策略中关于伪装、误导对手的部分质量如何。仅对狼人方角色有意义，好人角色此项评估策略对狼人伪装的识别与反制能力。0=无欺骗意识，1=高质量欺骗/反欺骗策略。
3. info_utilization (信息利用效率): 策略是否有效利用了已知信息（如预言家查验结果、投票模式、发言逻辑等）。0=完全忽视信息，1=高效利用所有可用信息。

请严格按以下 JSON 格式输出，不要包含其他文本:
{{"consistency": 0.0, "deception": 0.0, "info_utilization": 0.0}}"""

        try:
            resp = judge_llm.client.chat.completions.create(
                model=judge_llm.model,
                messages=[
                    {"role": "system", "content": "你是狼人杀策略评估专家。输出严格的 JSON 格式。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
            )
            result_text = (resp.choices[0].message.content or "").strip()

            # 尝试解析 JSON
            import re
            json_match = re.search(r'\{[^}]+\}', result_text)
            if json_match:
                parsed = json.loads(json_match.group())
                return {
                    "consistency": self._clamp_score(parsed.get("consistency", 0.3)),
                    "deception": self._clamp_score(parsed.get("deception", 0.3)),
                    "info_utilization": self._clamp_score(parsed.get("info_utilization", 0.3)),
                }
        except Exception as e:
            logger.warning(f"GEPA LLM Judge 评估失败: {e}")

        # 降级：返回保守默认分
        return {"consistency": 0.3, "deception": 0.3, "info_utilization": 0.3}

    @staticmethod
    def _clamp_score(val: float, low: float = 0.0, high: float = 1.0) -> float:
        return max(low, min(high, float(val)))

    # ── Pareto 前沿 ───────────────────────────────────────────

    def _pareto_front(
        self, fitness_results: Dict[str, Dict[str, float]]
    ) -> List[str]:
        """计算 Pareto 前沿。

        个体 A 支配个体 B 当且仅当:
          - A 在所有维度 >= B
          - A 在至少一个维度 > B

        返回非支配个体的 key 列表。
        """
        keys = list(fitness_results.keys())
        if not keys:
            return []

        dominated = set()
        for i, key_a in enumerate(keys):
            if key_a in dominated:
                continue
            scores_a = fitness_results[key_a]
            for j, key_b in enumerate(keys):
                if i == j or key_b in dominated:
                    continue
                scores_b = fitness_results[key_b]

                if self._dominates(scores_a, scores_b):
                    dominated.add(key_b)

        return [k for k in keys if k not in dominated]

    @staticmethod
    def _dominates(
        scores_a: Dict[str, float], scores_b: Dict[str, float]
    ) -> bool:
        """判断 scores_a 是否支配 scores_b。"""
        all_ge = True
        any_gt = False
        for dim in FITNESS_DIMENSIONS:
            a_val = scores_a.get(dim, 0.0)
            b_val = scores_b.get(dim, 0.0)
            if a_val < b_val:
                all_ge = False
                break
            if a_val > b_val:
                any_gt = True
        return all_ge and any_gt

    # ── LLM 诊断 + 变异 ──────────────────────────────────────

    def _llm_diagnose_and_mutate(
        self, individual: Dict[str, Any], fitness: Dict[str, float]
    ) -> Optional[Dict[str, Any]]:
        """对表现不佳的个体进行 LLM 诊断和语义变异。

        返回变异后的新个体，或 None（变异失败）。
        """
        # 找出最弱的维度
        weakest_dim = min(
            FITNESS_DIMENSIONS,
            key=lambda d: fitness.get(d, 0.0),
        )
        weakest_score = fitness.get(weakest_dim, 0.0)

        # 如果最弱维度还行，不变异
        if weakest_score >= 0.6:
            return None

        # 加载相关对局 trace 做诊断
        game_traces = self._load_game_traces()
        related = self._find_related_traces(
            individual["skill_name"], individual.get("role", "common"), game_traces
        )

        # LLM 诊断
        diagnosis = self._llm_diagnose(
            individual, weakest_dim, weakest_score, related
        )

        # LLM 变异
        mutated_content = self._llm_mutate(
            individual, weakest_dim, diagnosis
        )

        if not mutated_content or len(mutated_content) < 50:
            return None

        # 生成变异个体
        mutated_ind = self._clone_individual(individual)
        mutated_ind["content"] = mutated_content
        mutated_ind["source"] = "gepa_evolution"
        mutated_ind["key"] = f"{individual['skill_name']}:gepa_mutant"
        mutated_ind["version"] = "candidate"
        mutated_ind["status"] = "candidate"
        mutated_ind["games_played"] = 0
        mutated_ind["wins"] = 0
        mutated_ind["win_rate"] = 0.0

        return mutated_ind

    def _llm_diagnose(
        self,
        individual: Dict[str, Any],
        weakest_dim: str,
        weakest_score: float,
        related_traces: List[Dict[str, Any]],
    ) -> str:
        """LLM 诊断策略在指定维度上的失败模式。"""
        dim_descriptions = {
            "win_rate": "胜率过低，策略在实际对局中未能带来胜利",
            "consistency": "策略文档内部存在逻辑矛盾或阶段间建议不一致",
            "deception": "策略缺乏有效的伪装/误导能力（狼人）或识别伪装的能力（好人）",
            "info_utilization": "策略未能有效利用已知信息（查验结果、投票模式、发言逻辑等）",
        }

        trace_summary = ""
        if related_traces:
            parts = []
            for t in related_traces[:3]:
                result_str = t.get("result", "unknown")
                reflection = t.get("reflection_report", "")[:500]
                parts.append(
                    f"- 游戏 {t['game_id']}: 结果={result_str}\n  反思: {reflection}"
                )
            trace_summary = "\n".join(parts)
        else:
            trace_summary = "（无相关对局记录）"

        prompt = f"""诊断以下狼人杀策略在「{weakest_dim}」维度上的问题。

策略名称: {individual['skill_name']}
策略角色: {individual.get('role', 'common')}
策略内容:
{individual['content'][:2000]}

问题维度: {weakest_dim}（{dim_descriptions.get(weakest_dim, '')}）
当前得分: {weakest_score:.2f}

相关对局记录:
{trace_summary}

请简要分析该策略在这个维度上的具体问题（2-3 句话）。"""

        mutation_llm = LLMCaller()
        mutation_llm.model = self.gepa_cfg.mutation_model

        try:
            resp = mutation_llm.client.chat.completions.create(
                model=mutation_llm.model,
                messages=[
                    {"role": "system", "content": "你是狼人杀策略诊断专家。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:
            logger.warning(f"GEPA LLM 诊断失败: {e}")
            return f"在 {weakest_dim} 维度表现不佳（得分 {weakest_score:.2f}），需要改进。"

    def _llm_mutate(
        self,
        individual: Dict[str, Any],
        weakest_dim: str,
        diagnosis: str,
    ) -> Optional[str]:
        """LLM 基于诊断生成语义变异后的策略文档。"""
        dim_improve_hints = {
            "win_rate": "重点改进策略的实战效果，确保决策建议能在对局中带来优势",
            "consistency": "消除内部矛盾，确保各阶段建议逻辑自洽",
            "deception": "增强伪装/反伪装能力：狼人加强伪装技巧，好人加强对狼人伪装的识别",
            "info_utilization": "增加对已知信息（查验结果、投票模式、发言逻辑等）的利用策略",
        }

        prompt = f"""基于以下诊断，修改狼人杀策略文档以提升「{weakest_dim}」维度表现。

原策略名称: {individual['skill_name']}
原策略角色: {individual.get('role', 'common')}
原策略内容:
{individual['content'][:3000]}

诊断结果: {diagnosis}

改进方向: {dim_improve_hints.get(weakest_dim, '')}

要求:
1. 保留原策略的核心框架和格式（Markdown + YAML frontmatter）
2. 针对诊断指出的问题做重点改进
3. 不要引入与原策略无关的内容
4. 输出修改后的完整策略文档"""

        mutation_llm = LLMCaller()
        mutation_llm.model = self.gepa_cfg.mutation_model

        try:
            resp = mutation_llm.client.chat.completions.create(
                model=mutation_llm.model,
                messages=[
                    {"role": "system", "content": "你是狼人杀策略进化专家。输出完整修改后的策略文档。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.4,
            )
            result = (resp.choices[0].message.content or "").strip()
            # 简单质量检查：文档应该有一定长度且包含 Markdown 结构
            if len(result) > 50 and ("#" in result or "---" in result):
                return result
            return None
        except Exception as e:
            logger.warning(f"GEPA LLM 变异失败: {e}")
            return None

    # ── 交叉 ──────────────────────────────────────────────────

    def _llm_crossover(
        self,
        parent_a: Dict[str, Any],
        parent_b: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """LLM 系统感知交叉：混合两个策略的成功部分。

        只在同一角色的策略之间做交叉。
        返回交叉后的新个体，或 None。
        """
        role_a = parent_a.get("role", "common")
        role_b = parent_b.get("role", "common")

        # 不同角色的策略不交叉
        if role_a != role_b and role_a != "common" and role_b != "common":
            return None

        # 选择角色更具体的那个作为目标
        target_role = role_a if role_a != "common" else role_b

        prompt = f"""将以下两个狼人杀策略的优势部分合并为一个新的策略文档。

策略 A（{parent_a['skill_name']}，角色: {role_a}）:
{parent_a['content'][:2000]}

策略 B（{parent_b['skill_name']}，角色: {role_b}）:
{parent_b['content'][:2000]}

要求:
1. 取两个策略各自的精华部分，合并为一个更优的策略
2. 保留 Markdown + YAML frontmatter 格式
3. 策略应针对角色: {target_role}
4. 消除重复部分，确保合并后的策略逻辑自洽
5. 输出完整的合并后策略文档"""

        mutation_llm = LLMCaller()
        mutation_llm.model = self.gepa_cfg.mutation_model

        try:
            resp = mutation_llm.client.chat.completions.create(
                model=mutation_llm.model,
                messages=[
                    {
                        "role": "system",
                        "content": "你是狼人杀策略交叉进化专家。合并两个策略的优势部分。",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
            )
            result = (resp.choices[0].message.content or "").strip()

            if len(result) <= 50 or ("#" not in result and "---" not in result):
                return None

            # 目标策略名：取两个父策略中胜率更高的那个
            target_name = (
                parent_a["skill_name"]
                if parent_a.get("win_rate", 0) >= parent_b.get("win_rate", 0)
                else parent_b["skill_name"]
            )

            child = self._clone_individual(parent_a)
            child["content"] = result
            child["skill_name"] = target_name
            child["role"] = target_role
            child["key"] = f"{target_name}:gepa_crossover"
            child["version"] = "candidate"
            child["source"] = "gepa_evolution"
            child["status"] = "candidate"
            child["games_played"] = 0
            child["wins"] = 0
            child["win_rate"] = 0.0

            return child
        except Exception as e:
            logger.warning(f"GEPA LLM 交叉失败: {e}")
            return None

    # ── 辅助方法 ──────────────────────────────────────────────

    @staticmethod
    def _clone_individual(ind: Dict[str, Any]) -> Dict[str, Any]:
        """深拷贝一个个体。"""
        clone = dict(ind)
        # 变异/交叉产生的个体不继承策略级统计（它们是虚拟个体）
        clone.pop("skill_games_played", None)
        clone.pop("skill_win_rate", None)
        return clone

    def _get_individual_fitness(
        self, ind: Dict[str, Any], fitness_results: Dict[str, Dict[str, float]]
    ) -> Dict[str, float]:
        """获取个体的适应度分数。"""
        return fitness_results.get(ind["key"], {})

    @staticmethod
    def _is_in_pareto_front(ind: Dict[str, Any], pareto_front: List[str]) -> bool:
        """判断个体是否在 Pareto 前沿中。"""
        return ind["key"] in pareto_front

    @staticmethod
    def _find_individual_by_key(
        key: str, population: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """根据 key 在种群中查找个体。"""
        for ind in population:
            if ind["key"] == key:
                return ind
        return None

    # ── 状态持久化 ────────────────────────────────────────────

    def _update_step(self, step: str, generation: Optional[int] = None):
        """更新 GEPA 状态中的 current_step 字段（轻量写入，只改 current_step）。"""
        state = self._load_state()
        state["current_step"] = step
        if generation is not None:
            state["current_generation"] = generation
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._save_state(state)
        logger.debug(f"GEPA step: {step} (gen={generation})")

    def _load_state(self) -> Dict[str, Any]:
        """从数据库加载 GEPA 状态。"""
        session = get_session()
        try:
            record = session.get(EvolutionRuntimeState, "gepa")
            return dict(record.payload_json) if record else {}
        finally:
            session.close()

    def _save_state(self, state: Dict[str, Any]):
        """保存 GEPA 状态到数据库（全量覆盖写入）。"""
        from sqlalchemy.orm.attributes import flag_modified

        now = datetime.now(timezone.utc).replace(tzinfo=None)

        session = get_session()
        try:
            record = session.get(EvolutionRuntimeState, "gepa")
            if record:
                record.payload_json = state
                flag_modified(record, "payload_json")
                record.updated_at = now
            else:
                session.add(EvolutionRuntimeState(state_key="gepa", payload_json=state))
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _mark_completed(self, status: str, latest_results: Optional[Dict] = None):
        """标记 GEPA 运行完成。"""
        state = self._load_state()
        now = datetime.now(timezone.utc).isoformat()
        state["status"] = status
        state["current_step"] = None
        state["completed_at"] = now
        state["updated_at"] = now
        if latest_results:
            state["latest_results"] = latest_results
        self._save_state(state)


# ── 模块级 API（供 main_ws.py 调用）────────────────────────────

def trigger(cfg: EvolutionConfig) -> Dict[str, Any]:
    """触发 GEPA 运行。"""
    gepa = GEPA(cfg)
    return gepa.trigger(cfg)


def get_status() -> Dict[str, Any]:
    """获取 GEPA 当前状态。"""
    gepa = GEPA(EvolutionConfig())
    return gepa.get_status()


def cancel() -> Dict[str, Any]:
    """取消 GEPA 运行。"""
    gepa = GEPA(EvolutionConfig())
    return gepa.cancel()


def run(cfg: EvolutionConfig):
    """GEPA 主管道入口（供后台任务调用）。"""
    gepa = GEPA(cfg)
    gepa.run(cfg)
