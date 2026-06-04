"""evolution/summary.py — LLM 驱动的自进化活动摘要生成

聚合自进化系统的各类活动（策略版本确认、版本竞争、缓冲池确认、
策略缺口、策展人行动、GEPA 离线进化、近期对局统计），
调用 LLM 生成中文可读摘要并持久化至 evolution_runtime_state。
"""
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

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
                    "version": v.version,
                    "win_rate": float(v.win_rate or 0),
                    "games_played": v.games_played,
                })

            # GEPA 运行状态
            record = session.get(EvolutionRuntimeState, "gepa")
            gepa_state = dict(record.payload_json) if record else {}

            return {
                "new_versions": new_versions,
                "state": gepa_state,
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
            "你是狼人杀 AI 自进化系统的摘要撰写助手。"
            "你的任务是根据系统活动的结构化数据，撰写一段自然流畅的中文摘要。"
            "摘要应面向人类读者，使用叙事风格，避免生硬的枚举和符号。"
            "如果某个分类在统计周期内没有活动，则不需要提及。"
            "请按以下顺序组织内容：\n"
            "1. 策略更新（哪些策略被更新、从哪个版本到哪个版本、基于多少建议）\n"
            "2. 版本竞争（哪些候选版本胜率达标、晋升或降级情况）\n"
            "3. 缓冲池确认（哪些集群被确认、一致率和因果强度如何）\n"
            "4. 策略缺口（哪些场景缺乏策略覆盖、出现频次）\n"
            "5. 策展人行动（上次运行时间、做了什么操作）\n"
            "6. GEPA 离线进化（产生了哪些新版本）\n"
            "7. 近期对局统计（总对局数、胜负比、各角色表现）\n\n"
            "写作要求：\n"
            "- 使用自然流畅的中文叙述，像向团队成员汇报一样\n"
            "- 用具体的数字和事实支撑每条陈述\n"
            "- 避免使用技术术语缩写或英文标识符，优先使用中文描述\n"
            "- 每段聚焦一个主题，段落之间自然衔接\n"
            "- 开头用一句话概括统计周期"
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
            return resp.choices[0].message.content or ""
        except Exception:
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
        state = data.get("state", {})
        if not new_versions and not state:
            return "（本周期内无 GEPA 离线进化活动）"
        lines = []
        if new_versions:
            for v in new_versions:
                lines.append(
                    f"- 策略「{v['skill_name']}」版本 {v['version']}，"
                    f"对局数 {v['games_played']}，胜率 {v['win_rate']:.0%}"
                )
        if state:
            lines.append(f"GEPA 运行状态: {state}")
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

        parts = [f"自进化系统活动摘要（{since_str} 至 {now_str}）："]

        new_versions = activities.get("new_versions", [])
        if new_versions:
            names = [f"「{v['skill_name']}」{v['version']}" for v in new_versions]
            parts.append(f"策略更新: {len(new_versions)} 个策略产生了新版本（{', '.join(names)}）。")
        else:
            parts.append("策略更新: 本周期内无新版本确认。")

        promoted = activities.get("promoted_versions", [])
        active_count = sum(1 for v in promoted if v["status"] == "active")
        superseded_count = sum(1 for v in promoted if v["status"] == "superseded")
        if promoted:
            parts.append(f"版本竞争: {active_count} 个版本晋升，{superseded_count} 个版本被取代。")

        confirmed = activities.get("confirmed_clusters", [])
        if confirmed:
            parts.append(f"缓冲池确认: {len(confirmed)} 个集群通过确认阈值。")

        gaps = activities.get("strategy_gaps", [])
        if gaps:
            parts.append(f"策略缺口: {len(gaps)} 个场景缺乏策略覆盖。")

        curator = activities.get("curator_summary", {})
        if curator.get("last_run_at"):
            parts.append(f"策展人: 上次运行于 {curator['last_run_at']}。")

        gepa = activities.get("gepa_summary", {})
        if gepa.get("new_versions"):
            parts.append(f"GEPA 进化: 产生 {len(gepa['new_versions'])} 个新版本。")

        stats = activities.get("game_stats", {})
        total = stats.get("total", 0)
        if total > 0:
            win = stats.get("win", 0)
            parts.append(f"对局统计: 共 {total} 局，胜 {win} 局，胜率 {win / total:.0%}。")

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # 内部方法 — 持久化
    # ------------------------------------------------------------------

    def _persist(self, summary: Dict):
        """将摘要写入 evolution_runtime_state（key = latest_summary）。"""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        session = get_session()
        try:
            record = session.get(EvolutionRuntimeState, "latest_summary")
            if record:
                record.payload_json = summary
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
