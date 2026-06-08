"""evolution/summary.py — LLM 驱动的自进化活动摘要生成

聚合自进化系统的各类活动（策略版本确认、版本竞争、缓冲池确认、
策略缺口、策展人行动、GEPA 离线进化、近期对局统计），
调用 LLM 生成中文可读摘要并持久化至 evolution_runtime_state。
"""
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional
import logging

from sqlalchemy import func

from evolution.config import EvolutionConfig
from evolution.db import get_session
from evolution.models import (
    EvolutionSkill,
    EvolutionSkillVersion,
    EvolutionRuntimeState,
    EvolutionBufferItem,
    EvolutionStrategyGap,
    EvolutionGameArchive,
)
from agents.llm_caller import LLMCaller

logger = logging.getLogger("evolution.summary")


class EvolutionSummary:
    """自进化系统活动摘要生成器。"""

    def __init__(self, cfg: Optional[EvolutionConfig] = None):
        self.cfg = cfg

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate(self, cfg: EvolutionConfig, since: Optional[str] = None) -> Dict:
        """生成自进化活动摘要并持久化。

        Args:
            cfg: 进化配置。
            since: ISO 时间戳，聚合该时间之后的活跃数据。
                   若为 None 则取上一次摘要的 generated_at，仍无则取 7 天前。

        Returns:
            摘要字典，含 summary_text / generated_at / since / activities_snapshot。
        """
        if since is None:
            since = self._resolve_since()

        since_dt = datetime.fromisoformat(since)

        # 1. 聚合各类活动
        activities = self._aggregate_activities(since_dt)

        # 2. 调用 LLM 生成可读摘要
        summary_text = self._call_llm(cfg, since, activities)

        # 3. 组装并持久化
        now = datetime.now(timezone.utc)
        result = {
            "summary_text": summary_text,
            "generated_at": now.isoformat(),
            "since": since,
            "activities_snapshot": activities,
        }

        self._persist(result)
        return result

    def get_latest(self) -> Optional[Dict]:
        """从 evolution_runtime_state 加载最近一次摘要。无则返回 None。"""
        session = get_session()
        try:
            record = session.get(EvolutionRuntimeState, "latest_summary")
            return dict(record.payload_json) if record else None
        finally:
            session.close()

    # ------------------------------------------------------------------
    # 内部方法 — 时间窗口
    # ------------------------------------------------------------------

    def _resolve_since(self) -> str:
        """确定聚合起始时间：上次摘要 generated_at，否则 7 天前。"""
        latest = self.get_latest()
        if latest and latest.get("generated_at"):
            return latest["generated_at"]
        return (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    # ------------------------------------------------------------------
    # 内部方法 — 活动聚合
    # ------------------------------------------------------------------

    def _aggregate_activities(self, since_dt: datetime) -> Dict:
        """从数据库聚合各类自进化活动。"""
        return {
            "new_versions": self._agg_new_versions(since_dt),
            "promoted_versions": self._agg_promoted_versions(since_dt),
            "confirmed_clusters": self._agg_confirmed_clusters(since_dt),
            "strategy_gaps": self._agg_strategy_gaps(),
            "curator_summary": self._agg_curator(),
            "gepa_summary": self._agg_gepa(since_dt),
            "game_stats": self._agg_game_stats(since_dt),
        }

    # --- 1. 策略版本确认 ---
    def _agg_new_versions(self, since_dt: datetime) -> List[Dict]:
        """source in (debounced_update, manual_force_confirm) 且 created_at >= since 的新版本。"""
        session = get_session()
        try:
            rows = (
                session.query(EvolutionSkillVersion)
                .filter(
                    EvolutionSkillVersion.source.in_(["debounced_update", "manual_force_confirm"]),
                    EvolutionSkillVersion.created_at >= since_dt,
                )
                .all()
            )
            results = []
            for v in rows:
                skill = session.query(EvolutionSkill).filter_by(id=v.skill_id).first()
                results.append({
                    "skill_name": skill.skill_name if skill else str(v.skill_id),
                    "version": v.version,
                    "source": v.source,
                    "trigger_cluster_id": v.trigger_cluster_id,
                    "created_at": v.created_at.isoformat() if v.created_at else "",
                })
            return results
        finally:
            session.close()

    # --- 2. 版本竞争结果 ---
    def _agg_promoted_versions(self, since_dt: datetime) -> List[Dict]:
        """status 在 since 后变为 active 或 superseded 的版本。"""
        session = get_session()
        try:
            rows = (
                session.query(EvolutionSkillVersion)
                .filter(
                    EvolutionSkillVersion.status.in_(["active", "superseded"]),
                    EvolutionSkillVersion.updated_at >= since_dt,
                )
                .all()
            )
            results = []
            for v in rows:
                skill = session.query(EvolutionSkill).filter_by(id=v.skill_id).first()
                results.append({
                    "skill_name": skill.skill_name if skill else str(v.skill_id),
                    "version": v.version,
                    "status": v.status,
                    "games_played": v.games_played,
                    "wins": v.wins,
                    "win_rate": float(v.win_rate or 0),
                })
            return results
        finally:
            session.close()

    # --- 3. 缓冲池确认 ---
    def _agg_confirmed_clusters(self, since_dt: datetime) -> List[Dict]:
        """item_type = confirmed 且 updated_at >= since 的缓冲池条目。"""
        session = get_session()
        try:
            rows = (
                session.query(EvolutionBufferItem)
                .filter(
                    EvolutionBufferItem.item_type == "confirmed",
                    EvolutionBufferItem.updated_at >= since_dt,
                )
                .all()
            )
            results = []
            for item in rows:
                results.append({
                    "cluster_id": item.cluster_id or "",
                    "target_skill_name": item.target_skill_name or "",
                    "suggestion_count": item.suggestion_count,
                    "consistency_rate": float(item.consistency_rate or 0),
                    "avg_causal_strength": float(item.avg_causal_strength or 0),
                })
            return results
        finally:
            session.close()

    # --- 4. 策略缺口 ---
    def _agg_strategy_gaps(self) -> List[Dict]:
        """gap_count >= 3 的策略缺口。"""
        session = get_session()
        try:
            rows = (
                session.query(EvolutionStrategyGap)
                .filter(EvolutionStrategyGap.gap_count >= 3)
                .all()
            )
            return [
                {
                    "scene_description": g.scene_description,
                    "gap_count": g.gap_count,
                }
                for g in rows
            ]
        finally:
            session.close()

    # --- 5. Curator 行动 ---
    def _agg_curator(self) -> Dict:
        """从 evolution_runtime_state 读取 curator 状态。"""
        session = get_session()
        try:
            record = session.get(EvolutionRuntimeState, "curator")
            if not record:
                return {"last_run_at": None, "actions": {}}
            payload = dict(record.payload_json)
            return {
                "last_run_at": payload.get("last_run_at"),
                "actions": {
                    "staled": payload.get("staled", []),
                    "archived": payload.get("archived", []),
                    "patched": payload.get("patched", []),
                    "consolidated": payload.get("consolidated", []),
                },
            }
        finally:
            session.close()

    # --- 6. GEPA 离线进化 ---
    def _agg_gepa(self, since_dt: datetime) -> Dict:
        """source = gepa_evolution 的新版本 + gepa 运行状态。"""
        session = get_session()
        try:
            # GEPA 产生的新版本
            rows = (
                session.query(EvolutionSkillVersion)
                .filter(
                    EvolutionSkillVersion.source == "gepa_evolution",
                    EvolutionSkillVersion.created_at >= since_dt,
                )
                .all()
            )
            new_versions = []
            for v in rows:
                skill = session.query(EvolutionSkill).filter_by(id=v.skill_id).first()
                new_versions.append({
                    "skill_name": skill.skill_name if skill else str(v.skill_id),
                    "role": skill.role if skill else "未知",
                    "version": v.version,
                    "win_rate": float(v.win_rate or 0),
                    "games_played": v.games_played,
                    "created_at": v.created_at.isoformat() if v.created_at else "",
                })

            # GEPA 运行状态
            record = session.get(EvolutionRuntimeState, "gepa")
            gepa_state = dict(record.payload_json) if record else {}

            # 从 GEPA history 提取代际摘要
            generations = []
            for h in gepa_state.get("history", []):
                gen = {
                    "generation": h.get("generation"),
                    "mutations": h.get("mutations", 0),
                    "crossovers": h.get("crossovers", 0),
                    "pareto_front": h.get("pareto_front", []),
                    "new_versions": h.get("new_versions_created", []),
                    "best_fitness": h.get("best_fitness", {}),
                }
                generations.append(gen)

            return {
                "new_versions": new_versions,
                "state": gepa_state,
                "generations": generations,
            }
        finally:
            session.close()

    # --- 7. 近期对局统计 ---
    def _agg_game_stats(self, since_dt: datetime) -> Dict:
        """统计 since 以来的对局数及胜负分布。"""
        session = get_session()
        try:
            rows = (
                session.query(EvolutionGameArchive)
                .filter(EvolutionGameArchive.created_at >= since_dt)
                .all()
            )
            total = len(rows)
            win_count = sum(1 for g in rows if g.result == "win")
            loss_count = sum(1 for g in rows if g.result == "loss")

            by_role: Dict[str, Dict] = {}
            for g in rows:
                role = g.my_role or "unknown"
                if role not in by_role:
                    by_role[role] = {"total": 0, "win": 0, "loss": 0}
                by_role[role]["total"] += 1
                if g.result == "win":
                    by_role[role]["win"] += 1
                elif g.result == "loss":
                    by_role[role]["loss"] += 1

            return {
                "total": total,
                "win": win_count,
                "loss": loss_count,
                "by_role": by_role,
            }
        finally:
            session.close()

    # ------------------------------------------------------------------
    # 内部方法 — LLM 调用
    # ------------------------------------------------------------------

    def _call_llm(self, cfg: EvolutionConfig, since: str, activities: Dict) -> str:
        """调用 LLM 生成中文可读摘要。"""
        llm = LLMCaller()
        llm.model = cfg.summary.model

        system_prompt = (
            "你是狼人杀 AI 自进化系统的技术写手。"
            "你的任务是根据系统活动的数据，写一段面向团队成员的中文总结。"
            "写作风格：像团队周报一样，用自然流畅的中文讲述系统做了什么、发现了什么、改善了什么。\n\n"
            "核心要求：\n"
            "- 不要列举数据表格或编号列表，用叙述性语言串联信息\n"
            "- 重点关注「变化」和「行动」，而非静态数据\n"
            "- 如果某个模块没有活动，简单带过即可，不要说「本周期内无...」\n"
            "- 开头用一句话概括本周期的整体节奏\n"
            "- 每段聚焦一个主题，段落间自然过渡\n"
            "- 用具体事实说话：哪个策略被更新了、因为什么、效果如何\n"
            "- 如果系统发现了问题（如策略缺口），说明问题是什么、意味着什么\n\n"
            "内容组织（按实际有内容的顺序写，没内容的跳过）：\n"
            "1. 策略更新动态 — 哪些策略被改进/新增，基于什么信号\n"
            "2. 版本竞争进展 — 候选版本表现如何，是否有版本晋升或降级\n"
            "3. 缓冲池动态 — 新的建议在积累，哪些集群被确认通过\n"
            "4. 发现的问题 — 策略缺口意味着什么场景缺乏指导\n"
            "5. 策展人工作 — 上次做了什么维护操作\n"
            "6. GEPA 进化 — 是否运行，产生了什么新策略，重点描述哪个角色的哪个策略"
            "被变异或交叉，以及 Pareto 前沿变化\n"
            "7. 整体对局表现 — 近期胜负趋势，各角色表现概览"
        )

        user_prompt = (
            f"统计周期起始时间：{since}\n\n"
            f"以下是自进化系统在该周期内的活动数据：\n\n"
            f"=== 策略版本确认 ===\n{self._format_new_versions(activities['new_versions'])}\n\n"
            f"=== 版本竞争结果 ===\n{self._format_promoted_versions(activities['promoted_versions'])}\n\n"
            f"=== 缓冲池确认 ===\n{self._format_confirmed_clusters(activities['confirmed_clusters'])}\n\n"
            f"=== 策略缺口 ===\n{self._format_strategy_gaps(activities['strategy_gaps'])}\n\n"
            f"=== 策展人行动 ===\n{self._format_curator(activities['curator_summary'])}\n\n"
            f"=== GEPA 离线进化 ===\n{self._format_gepa(activities['gepa_summary'])}\n\n"
            f"=== 近期对局统计 ===\n{self._format_game_stats(activities['game_stats'])}\n"
        )

        try:
            resp = llm.client.chat.completions.create(
                model=llm.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
            )
            content = resp.choices[0].message.content or ""
            if content and len(content) > 30:
                return content
            logger.warning(f"Summary LLM returned empty/short content: {content[:100]}")
            return self._fallback_summary(since, activities)
        except Exception as e:
            logger.warning(f"Summary LLM call failed: {e}")
            return self._fallback_summary(since, activities)

    # ------------------------------------------------------------------
    # 内部方法 — 数据格式化（供 LLM prompt 使用）
    # ------------------------------------------------------------------

    def _format_new_versions(self, items: List[Dict]) -> str:
        if not items:
            return "（本周期内无策略版本确认）"
        lines = []
        for v in items:
            cluster_info = f"，关联集群 {v['trigger_cluster_id']}" if v.get("trigger_cluster_id") else ""
            lines.append(
                f"- 策略「{v['skill_name']}」新增版本 {v['version']}，"
                f"来源: {v['source']}{cluster_info}，"
                f"创建时间: {v['created_at']}"
            )
        return "\n".join(lines)

    def _format_promoted_versions(self, items: List[Dict]) -> str:
        if not items:
            return "（本周期内无版本竞争结果）"
        lines = []
        for v in items:
            action = "晋升为活跃版本" if v["status"] == "active" else "被取代"
            lines.append(
                f"- 策略「{v['skill_name']}」版本 {v['version']} {action}，"
                f"对局数 {v['games_played']}，胜率 {v['win_rate']:.0%}"
            )
        return "\n".join(lines)

    def _format_confirmed_clusters(self, items: List[Dict]) -> str:
        if not items:
            return "（本周期内无缓冲池确认）"
        lines = []
        for c in items:
            target = c["target_skill_name"] or "未知策略"
            lines.append(
                f"- 集群 {c['cluster_id']} → 目标策略「{target}」，"
                f"建议数 {c['suggestion_count']}，"
                f"一致率 {c['consistency_rate']:.0%}，"
                f"平均因果强度 {c['avg_causal_strength']:.2f}"
            )
        return "\n".join(lines)

    def _format_strategy_gaps(self, items: List[Dict]) -> str:
        if not items:
            return "（当前无显著策略缺口）"
        lines = []
        for g in items:
            lines.append(f"- 场景「{g['scene_description']}」，出现 {g['gap_count']} 次")
        return "\n".join(lines)

    def _format_curator(self, data: Dict) -> str:
        last_run = data.get("last_run_at") or "从未运行"
        actions = data.get("actions", {})
        parts = [f"上次运行时间: {last_run}"]
        if actions:
            counts = []
            if actions.get("staled"):
                counts.append(f"标记过时 {len(actions['staled'])} 个")
            if actions.get("archived"):
                counts.append(f"归档 {len(actions['archived'])} 个")
            if actions.get("patched"):
                counts.append(f"修补 {len(actions['patched'])} 个")
            if actions.get("consolidated"):
                counts.append(f"合并 {len(actions['consolidated'])} 个")
            if counts:
                parts.append("操作: " + "，".join(counts))
            else:
                parts.append("操作: 无变更")
        return "\n".join(parts)

    def _format_gepa(self, data: Dict) -> str:
        new_versions = data.get("new_versions", [])
        generations = data.get("generations", [])
        state = data.get("state", {})

        if not new_versions and not state:
            return "（本周期内无 GEPA 离线进化活动）"

        lines = []

        # GEPA 运行概况
        status = state.get("status", "idle")
        total_gens = state.get("total_generations", 0)
        current_gen = state.get("current_generation", 0)
        if status == "running":
            lines.append(f"GEPA 正在运行中（第 {current_gen}/{total_gens} 代）")
        elif status in ("completed", "cancelled"):
            completed_at = state.get("completed_at", "")
            lines.append(f"GEPA 已{('完成' if status == 'completed' else '取消')}，共完成 {len(generations)} 代" + (f"，完成时间 {completed_at[:19]}" if completed_at else ""))

        # 代际详情
        if generations:
            lines.append("")
            lines.append("各代进化详情：")
            for g in generations:
                gen_num = g.get("generation", "?")
                mut = g.get("mutations", 0)
                cross = g.get("crossovers", 0)
                new_vers = g.get("new_versions", [])
                pareto = g.get("pareto_front", [])
                fitness = g.get("best_fitness", {})

                # 解析策略角色
                skills_by_role: Dict[str, list] = {}
                for v_name in new_vers:
                    # 版本名格式: skill-name:version
                    skill_name = v_name.rsplit(":", 1)[0] if ":" in v_name else v_name
                    role = "未知"
                    for nv in new_versions:
                        if nv["skill_name"] == skill_name:
                            role = nv["role"]
                            break
                    if role not in skills_by_role:
                        skills_by_role[role] = []
                    skills_by_role[role].append(v_name)

                # 同理解析 Pareto 前沿
                pareto_skills = []
                for p in pareto:
                    skill_name = p.rsplit(":", 1)[0] if ":" in p else p
                    pareto_skills.append(skill_name)

                parts = [f"第 {gen_num} 代："]
                ops = []
                if mut:
                    ops.append(f"{mut} 次变异")
                if cross:
                    ops.append(f"{cross} 次交叉")
                if ops:
                    parts.append(f"执行了 {'、'.join(ops)}；")

                if skills_by_role:
                    role_parts = []
                    for role, vers in skills_by_role.items():
                        role_parts.append(f"{role}角色策略「{vers[0].rsplit(':', 1)[0] if ':' in vers[0] else vers[0]}」产生 {len(vers)} 个新版本")
                    parts.append("、".join(role_parts) + "；")

                if pareto_skills:
                    unique_skills = list(dict.fromkeys(pareto_skills))
                    parts.append(f"Pareto 前沿 {len(pareto)} 个策略（{', '.join(unique_skills[:5])}）")

                if fitness:
                    wr = fitness.get("win_rate")
                    if wr is not None:
                        parts.append(f"，最佳胜率 {wr:.0%}")

                lines.append("".join(parts))

        # 新版本汇总
        if new_versions:
            lines.append("")
            lines.append("GEPA 产出的新版本：")
            by_role: Dict[str, list] = {}
            for v in new_versions:
                r = v.get("role", "未知")
                if r not in by_role:
                    by_role[r] = []
                by_role[r].append(v)
            for role, vers in by_role.items():
                names = [f"{v['version']}（胜率 {v['win_rate']:.0%}）" for v in vers]
                lines.append(f"  - {role}：{', '.join(names)}")

        return "\n".join(lines)

    def _format_game_stats(self, data: Dict) -> str:
        total = data.get("total", 0)
        if total == 0:
            return "（本周期内无对局记录）"
        win = data.get("win", 0)
        loss = data.get("loss", 0)
        lines = [
            f"总对局数: {total}，胜: {win}，负: {loss}，"
            f"总胜率: {win / total:.0%}"
        ]
        by_role = data.get("by_role", {})
        if by_role:
            lines.append("各角色表现:")
            for role, stats in by_role.items():
                r_total = stats["total"]
                r_win = stats["win"]
                lines.append(
                    f"  - {role}: {r_total} 局，胜 {r_win}，"
                    f"胜率 {r_win / r_total:.0%}" if r_total > 0 else f"  - {role}: 0 局"
                )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 内部方法 — 降级摘要（LLM 不可用时）
    # ------------------------------------------------------------------

    def _fallback_summary(self, since: str, activities: Dict) -> str:
        """LLM 不可用时的简单模板摘要。"""
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        since_str = datetime.fromisoformat(since).strftime("%Y-%m-%d")

        parts = [f"自 {since_str} 至 {now_str}，自进化系统运行概况如下："]

        new_versions = activities.get("new_versions", [])
        if new_versions:
            names = [f"「{v['skill_name']}」{v['version']}" for v in new_versions]
            parts.append(f"系统完成了 {len(new_versions)} 个策略的版本更新（{', '.join(names)}），这些更新源自对局反思后的建议确认。")
        else:
            parts.append("本周期内策略版本库没有新增确认。")

        promoted = activities.get("promoted_versions", [])
        active_count = sum(1 for v in promoted if v["status"] == "active")
        superseded_count = sum(1 for v in promoted if v["status"] == "superseded")
        if promoted:
            parts.append(f"版本竞争方面，{active_count} 个候选版本胜率达标成功晋升为活跃版本，{superseded_count} 个旧版本被替代。")

        confirmed = activities.get("confirmed_clusters", [])
        if confirmed:
            parts.append(f"缓冲池中有 {len(confirmed)} 个建议集群通过了确认阈值，将进入策略更新管道。")

        gaps = activities.get("strategy_gaps", [])
        if gaps:
            gap_desc = "、".join(g["scene_description"] for g in gaps[:3])
            parts.append(f"系统发现 {len(gaps)} 个策略缺口，说明在「{gap_desc}」等场景下还缺乏有效的策略指导。")

        curator = activities.get("curator_summary", {})
        if curator.get("last_run_at"):
            run_time = curator.get("last_run_at", "")
            actions = curator.get("actions", {})
            action_parts = []
            if actions.get("staled"):
                action_parts.append(f"标记过时 {len(actions['staled'])} 个")
            if actions.get("archived"):
                action_parts.append(f"归档 {len(actions['archived'])} 个")
            if actions.get("patched"):
                action_parts.append(f"修补 {len(actions['patched'])} 个")
            if actions.get("consolidated"):
                action_parts.append(f"合并 {len(actions['consolidated'])} 个")
            if action_parts:
                parts.append(f"策展人上次运行于 {run_time[:10]}，执行了{'、'.join(action_parts)}等维护操作。")
            else:
                parts.append(f"策展人上次运行于 {run_time[:10]}，本轮未做变更。")

        gepa = activities.get("gepa_summary", {})
        if gepa.get("new_versions"):
            parts.append(f"GEPA 离线进化在本周期产生了 {len(gepa['new_versions'])} 个新策略版本。")

        stats = activities.get("game_stats", {})
        total = stats.get("total", 0)
        if total > 0:
            win = stats.get("win", 0)
            parts.append(f"近期共完成 {total} 局对局，胜 {win} 局，总胜率 {win / total:.0%}。")
            by_role = stats.get("by_role", {})
            if by_role:
                role_parts = []
                for role, rs in by_role.items():
                    if rs["total"] > 0:
                        role_parts.append(f"{role}{rs['win'] / rs['total']:.0%}胜率")
                if role_parts:
                    parts.append(f"各角色表现：{'、'.join(role_parts)}。")
        else:
            parts.append("本周期内暂无对局记录。")

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # 内部方法 — 持久化
    # ------------------------------------------------------------------

    def _persist(self, summary: Dict):
        """将摘要写入 evolution_runtime_state（key = latest_summary）。"""
        from sqlalchemy.orm.attributes import flag_modified

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        session = get_session()
        try:
            record = session.get(EvolutionRuntimeState, "latest_summary")
            if record:
                record.payload_json = summary
                flag_modified(record, "payload_json")
                record.updated_at = now
            else:
                session.add(EvolutionRuntimeState(
                    state_key="latest_summary", payload_json=summary
                ))
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
