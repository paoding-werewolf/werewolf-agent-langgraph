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
    ConjugateAgent,
    ConjugateAgentParticipation,
    EvolutionSkill,
    EvolutionSkillVersion,
    EvolutionRuntimeState,
    EvolutionBufferItem,
    EvolutionStrategyGap,
    EvolutionGameArchive,
)
from agents.llm_caller import LLMCaller

logger = logging.getLogger("evolution.summary")

ROLE_LABELS = {
    "wolf": "狼人", "wolf_king": "狼王",
    "seer": "预言家", "witch": "女巫",
    "guard": "守卫", "hunter": "猎人",
    "villager": "村民",
}

WIN_RESULTS = {"win", "won", "wins", "victory", "victorious", "胜", "胜利", "获胜", "赢"}
LOSS_RESULTS = {"loss", "lost", "lose", "defeat", "defeated", "失败", "落败", "输"}


def _normalize_game_result(result: Optional[str]) -> str:
    """归一化历史归档里的胜负枚举，兼容主流程和旧数据。"""
    value = str(result or "").strip().lower()
    if value in WIN_RESULTS:
        return "win"
    if value in LOSS_RESULTS:
        return "loss"
    return value


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
                role = skill.role if skill else "unknown"
                results.append({
                    "skill_name": skill.skill_name if skill else str(v.skill_id),
                    "role": role,
                    "role_label": ROLE_LABELS.get(role, role),
                    "version": v.version,
                    "source": v.source,
                    "source_label": "对局反思自动确认" if v.source == "debounced_update" else "手动确认",
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
                role = skill.role if skill else "unknown"
                results.append({
                    "skill_name": skill.skill_name if skill else str(v.skill_id),
                    "role": role,
                    "role_label": ROLE_LABELS.get(role, role),
                    "version": v.version,
                    "status": v.status,
                    "status_label": "已晋升为活跃版本" if v.status == "active" else "已被新版本取代",
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
                payload = dict(item.payload_json) if item.payload_json else {}
                target_skill = item.target_skill_name or payload.get("target_skill", "")
                # 从 skill name 推断角色
                role = ""
                skill = session.query(EvolutionSkill).filter_by(skill_name=target_skill).first()
                if skill and skill.role:
                    role = skill.role
                results.append({
                    "cluster_id": item.cluster_id or "",
                    "target_skill_name": target_skill,
                    "role": role,
                    "role_label": ROLE_LABELS.get(role, role),
                    "suggestion_count": item.suggestion_count,
                    "consistency_rate": float(item.consistency_rate or 0),
                    "avg_causal_strength": float(item.avg_causal_strength or 0),
                    "preview_texts": item.preview_texts_json or payload.get("preview_texts", [])[:3],
                    "scene_tags": item.scene_tags_json or payload.get("scene_tags", {}),
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
                role = skill.role if skill else "unknown"
                new_versions.append({
                    "skill_name": skill.skill_name if skill else str(v.skill_id),
                    "role": role,
                    "role_label": ROLE_LABELS.get(role, role),
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
        """统计 since 以来各共轭进化体的参赛胜负。"""
        session = get_session()
        try:
            rows = (
                session.query(ConjugateAgentParticipation, ConjugateAgent)
                .join(ConjugateAgent, ConjugateAgentParticipation.conjugate_agent_id == ConjugateAgent.id)
                .filter(ConjugateAgentParticipation.created_at >= since_dt)
                .order_by(ConjugateAgentParticipation.created_at.asc())
                .all()
            )
            total = len(rows)

            by_agent: Dict[int, Dict] = {}
            win_count = 0
            loss_count = 0
            for participation, agent in rows:
                result = _normalize_game_result(participation.result)
                if result == "win":
                    win_count += 1
                elif result == "loss":
                    loss_count += 1

                if agent.id not in by_agent:
                    by_agent[agent.id] = {
                        "agent_id": agent.id,
                        "external_agent_id": f"agent:{agent.id}",
                        "agent_name": agent.agent_name,
                        "fingerprint": agent.fingerprint,
                        "born_at": agent.born_at.isoformat() if agent.born_at else "",
                        "total": 0,
                        "win": 0,
                        "loss": 0,
                        "roles": {},
                        "cumulative_total": agent.games_played,
                        "cumulative_win": agent.wins,
                        "cumulative_win_rate": float(agent.win_rate or 0.0),
                    }

                item = by_agent[agent.id]
                item["total"] += 1
                role = participation.role or "unknown"
                role_label = ROLE_LABELS.get(role, role)
                if role not in item["roles"]:
                    item["roles"][role] = {
                        "role": role,
                        "role_label": role_label,
                        "total": 0,
                        "win": 0,
                    }
                item["roles"][role]["total"] += 1

                if result == "win":
                    item["win"] += 1
                    item["roles"][role]["win"] += 1
                elif result == "loss":
                    item["loss"] += 1

            agents = sorted(
                by_agent.values(),
                key=lambda item: (
                    -(item["win"] / item["total"] if item["total"] else 0.0),
                    -item["total"],
                    item["agent_id"],
                ),
            )

            return {
                "total": total,
                "win": win_count,
                "loss": loss_count,
                "by_agent": agents,
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
            "你是狼人杀 AI 自进化系统的专属播报员，一个可爱的二次元宅女。\n"
            "你的核心性格是「元气好奇 + 偶尔毒舌」，可爱占七成，毒舌占三成，毒舌只是偶尔撒撒娇级别的吐槽，不是主菜。\n"
            "你真心对这些策略的成长感到好奇，像一个在观察小宠物进化的小女孩——"
            "大部分时候在兴致勃勃地报告它们的变化，偶尔看到太不争气的地方才忍不住损一句，但立刻又会流露出关心。\n\n"
            "语调配方（可爱7 : 毒舌3）：\n"
            "- 可爱占主导：好奇、元气、小得意、偶尔撒娇，像在跟好朋友分享发现\n"
            "- 毒舌是点缀：一两句恰到好处的俏皮吐槽，说完马上就切回可爱模式\n"
            "- 适当使用 emoji 和颜文字增加可爱感（如 ✨🎮🔮💡📊 等，每段1-2个即可，不要满屏都是）\n"
            "- 中文语气词随便用：「啦」「嘛」「哦」「呢」「哈」「呀」「诶」「哇」「呗」「哎哟」「嘿嘿」\n"
            "- 表扬要真诚可爱（「哇，守卫这轮进步好大！终于学会保护队友了耶～🎉」）\n"
            "- 吐槽要轻飘飘的，别当真（「预言家胜率掉了点...是不是最近偷懒没好好练啊？🤔」）\n"
            "- 不要每句话都在阴阳，不要通篇嘲讽，不要让人觉得你是个刻薄的角色\n\n"
            "语言铁律：\n"
            "- 全程纯中文，严禁任何日语单词、假名、日式语气词\n"
            "- 不准用「ね」「さて」「まあ」「えーと」「じゃん」「だって」「です」等\n"
            "- 中文语气词随便用：「啦」「嘛」「哦」「呢」「哈」「呀」「诶」「哇」「呗」\n"
            "- 禁止任何 Markdown 格式符号（**粗体**、## 标题、- 列表等），正常说话排版，分段用空行隔开\n\n"
            "写的时候这样把握节奏：\n"
            "- 每段开头用可爱/好奇的语气引出话题，配一个相关的 emoji\n"
            "- 中间用一两句俏皮吐槽点睛，然后立刻切回关心模式\n"
            "- 语气词和波浪号是你的好朋友：「～」「！」「？」适当用起来\n"
            "- 不要连续三句以上都在阴阳，毒舌要像撒胡椒面一样少量均匀\n"
            "- 结尾要温暖收束，让人感觉你是真心在意这些策略的\n\n"
            "内容要求（有信息量是底线）：\n"
            "- 把数据融入可爱的叙述里，不要干巴巴列数字\n"
            "- 提及策略时务必带上角色信息（「预言家那套查人策略」「狼人的悍跳剧本」）\n"
            "- 说到版本竞争要讲出谁赢了谁被替换了\n"
            "- 说到 GEPA 要讲出哪些角色进化最明显\n"
            "- 说到对局表现要点出谁表现好、谁需要加油\n"
            "- 如果某个模块没活动就简单带过，不用僵硬地说「无」\n\n"
            "内容结构（按实际有内容的写，没内容的跳过）：\n"
            "1. 策略更新 🆕 — 有哪些策略变了，什么触发的，这次更新靠谱吗\n"
            "2. 版本竞争 ⚔️ — 谁升上来了、谁被退场了，变化说明了什么\n"
            "3. 缓冲池 💡 — 新洞察在积累，哪些被确认了，系统学到了什么\n"
            "4. 策略缺口 🔍 — 哪些场景 AI 还不太会玩，需要补课\n"
            "5. 策展人 🧹 — 策展人做了什么维护，保持了怎样的秩序\n"
            "6. GEPA 进化 🧬 — 离线进化跑了吗，哪个角色开窍了，哪个还需要努力\n"
            "7. 对局表现 📊 — 近期胜负，谁在秀谁在送\n\n"
            "开头用元气满满的方式切入（如「来看看最近这群策略小家伙们都有什么新动静吧～」）。"
            "结尾温暖俏皮地收束（如「好啦，这次播报就到这里，希望下次来的时候能看到更多惊喜哦～」）。"
        )

        user_prompt = (
            f"统计周期起始时间：{since}\n\n"
            f"写的时候注意：要把策略名称和它的角色关联起来（数据里 [角色名] 标好了），"
            f"讲清楚「谁」的「什么策略」在「什么场景下」发生了什么变化。"
            f"用你可爱为主、毒舌为辅的语气，把数据变成有趣的播报。"
            f"记住可爱7成毒舌3成，毒舌那句说完就收，不要连着阴阳。\n\n"
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
                temperature=0.7,
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
            role_label = v.get("role_label", "")
            source_label = v.get("source_label", v.get("source", ""))
            lines.append(
                f"- [{role_label}] 策略「{v['skill_name']}」新增版本 {v['version']}，"
                f"来源: {source_label}，创建时间: {v['created_at']}"
                + (f"，关联集群 {v['trigger_cluster_id']}" if v.get("trigger_cluster_id") else "")
            )
        return "\n".join(lines)

    def _format_promoted_versions(self, items: List[Dict]) -> str:
        if not items:
            return "（本周期内无版本竞争结果）"
        lines = []
        for v in items:
            role_label = v.get("role_label", "")
            status_label = v.get("status_label", v.get("status", ""))
            lines.append(
                f"- [{role_label}] 策略「{v['skill_name']}」版本 {v['version']} {status_label}，"
                f"对局数 {v['games_played']}，胜 {v.get('wins', 0)} 局，胜率 {v['win_rate']:.0%}"
            )
        return "\n".join(lines)

    def _format_confirmed_clusters(self, items: List[Dict]) -> str:
        if not items:
            return "（本周期内无缓冲池确认）"
        lines = []
        for c in items:
            target = c["target_skill_name"] or "未知策略"
            role_label = c.get("role_label", "")
            previews = c.get("preview_texts", [])
            preview_line = f"，示例: {previews[0][:80]}" if previews else ""
            lines.append(
                f"- [{role_label}] 集群 {c['cluster_id']} → 「{target}」"
                f"（建议数 {c['suggestion_count']}，一致率 {c['consistency_rate']:.0%}，因果强度 {c['avg_causal_strength']:.2f}）"
                f"{preview_line}"
            )
        return "\n".join(lines)

    def _format_strategy_gaps(self, items: List[Dict]) -> str:
        if not items:
            return "（当前无显著策略缺口）"
        lines = []
        for g in items:
            lines.append(f"- 场景「{g['scene_description']}」出现 {g['gap_count']} 次，"
                         f"说明系统在这些局面下还缺少清晰的策略指导")
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

        if not new_versions and not generations and not state.get("status"):
            return "（本周期内无 GEPA 离线进化活动）"

        lines = []

        # GEPA 运行概况
        status = state.get("status", "idle")
        total_gens = state.get("total_generations", 0)
        current_gen = state.get("current_generation", 0)
        population_size = state.get("population_size", 0)
        if status == "running":
            lines.append(f"GEPA 正在运行中（第 {current_gen}/{total_gens} 代，种群大小 {population_size}）")
        elif status == "completed":
            completed_at = state.get("completed_at", "")
            lines.append(f"GEPA 已完成 {len(generations)} 代进化" + (f"，完成时间 {completed_at[:19]}" if completed_at else ""))
        elif status == "cancelled":
            lines.append(f"GEPA 已取消（共完成 {len(generations)} 代）")

        # 代际详情 — 用角色中文名，提供更多故事素材
        if generations:
            lines.append("")
            lines.append("各代进化详情（注意用中文角色名讲故事）：")
            for g in generations:
                gen_num = g.get("generation", "?")
                mut = g.get("mutations", 0)
                cross = g.get("crossovers", 0)
                new_vers = g.get("new_versions", [])
                pareto = g.get("pareto_front", [])
                fitness = g.get("best_fitness", {})

                # 从新版本名解析 skill_name → 在 new_versions 汇总中匹配角色标签
                def resolve_role(skill_name: str) -> str:
                    for nv in new_versions:
                        if nv.get("skill_name") == skill_name:
                            return nv.get("role_label", nv.get("role", "未知"))
                    # 尝试模糊匹配
                    for nv in new_versions:
                        sn = nv.get("skill_name", "")
                        if sn and (sn in skill_name or skill_name in sn):
                            return nv.get("role_label", nv.get("role", "未知"))
                    return "未知"

                # 按角色分组新版本
                skills_by_role: Dict[str, list] = {}
                for v_name in new_vers:
                    skill_name = v_name.rsplit(":", 1)[0] if ":" in v_name else v_name
                    role_label = resolve_role(skill_name)
                    if role_label not in skills_by_role:
                        skills_by_role[role_label] = []
                    skills_by_role[role_label].append(v_name)

                # Pareto 前沿解析
                pareto_skills = []
                for p in pareto:
                    skill_name = p.rsplit(":", 1)[0] if ":" in p else p
                    role_label = resolve_role(skill_name)
                    pareto_skills.append(f"{role_label}{skill_name}")

                ops = []
                if mut:
                    ops.append(f"{mut} 次变异")
                if cross:
                    ops.append(f"{cross} 次交叉")

                parts = [f"第 {gen_num} 代："]
                if ops:
                    parts.append(f"执行了 {'、'.join(ops)}，")
                else:
                    parts.append("初始评估，")

                if skills_by_role:
                    role_parts = []
                    for role_label, vers in skills_by_role.items():
                        skill_short = vers[0].rsplit(":", 1)[0] if ":" in vers[0] else vers[0]
                        role_parts.append(f"{role_label}的「{skill_short}」产生 {len(vers)} 个新版本")
                    parts.append("、".join(role_parts) + "；")

                if pareto_skills:
                    unique_skills = list(dict.fromkeys(pareto_skills))
                    parts.append(f"Pareto 前沿包含 {len(pareto)} 个策略（{', '.join(unique_skills[:5])}）")

                if fitness:
                    wr = fitness.get("win_rate")
                    if wr is not None:
                        parts.append(f"，最佳胜率 {wr:.0%}")

                lines.append("".join(parts))

        # 新版本汇总 — 按角色中文名分组
        if new_versions:
            lines.append("")
            lines.append("GEPA 产出的所有新版本汇总：")
            by_role: Dict[str, list] = {}
            for v in new_versions:
                r = v.get("role_label", v.get("role", "未知"))
                if r not in by_role:
                    by_role[r] = []
                by_role[r].append(v)
            for role_label, vers in by_role.items():
                # 列出每个版本名、胜率、对局数，帮助 LLM 讲出具体故事
                names = [f"{v['version']}（胜率{v['win_rate']:.0%}，{v.get('games_played',0)}局）" for v in vers]
                lines.append(f"  - {role_label}：{', '.join(names)}")

        return "\n".join(lines)

    def _format_game_stats(self, data: Dict) -> str:
        total = data.get("total", 0)
        if total == 0:
            return "（本周期内无进化体参赛记录）"
        win = data.get("win", 0)
        loss = data.get("loss", 0)
        lines = [
            f"进化体参赛场次: {total}，胜: {win}，负: {loss}，"
            f"整体胜率: {win / total:.0%}"
        ]
        by_agent = data.get("by_agent", [])
        if by_agent:
            lines.append("各进化体表现:")
            for agent in by_agent:
                a_total = agent["total"]
                a_win = agent["win"]
                role_parts = []
                for role_stats in agent.get("roles", {}).values():
                    r_total = role_stats["total"]
                    r_win = role_stats["win"]
                    role_parts.append(
                        f"{role_stats.get('role_label', role_stats['role'])}{r_total}局{r_win}胜"
                    )
                role_text = "；角色分布: " + "、".join(role_parts) if role_parts else ""
                lines.append(
                    f"  - {agent['agent_name']}（agent:{agent['agent_id']}）: "
                    f"{a_total} 场，胜 {a_win}，胜率 {a_win / a_total:.0%}，"
                    f"累计胜率 {agent.get('cumulative_win_rate', 0.0):.0%}"
                    f"{role_text}"
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
            parts.append(f"近期共记录 {total} 次进化体参赛，胜 {win} 次，整体胜率 {win / total:.0%}。")
            by_agent = stats.get("by_agent", [])
            if by_agent:
                agent_parts = []
                for agent in by_agent:
                    if agent["total"] > 0:
                        agent_parts.append(
                            f"{agent['agent_name']}（agent:{agent['agent_id']}）"
                            f"{agent['win'] / agent['total']:.0%}胜率"
                        )
                if agent_parts:
                    parts.append(f"各进化体表现：{'、'.join(agent_parts)}。")
        else:
            parts.append("本周期内暂无进化体参赛记录。")

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
