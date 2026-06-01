"""evolution/curator.py — 自主策展人

两阶段策略库维护：
  阶段一：确定性状态转移（active → stale → archived）
  阶段二：LLM 审查（keep / patch / consolidate / archive）
"""
import json
import yaml
import shutil
import tarfile
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime, timezone, timedelta

from evolution.config import EvolutionConfig, AGENT_HOME
from evolution.skill_loader import SkillLoader
from agents.llm_caller import LLMCaller


class Curator:
    def __init__(self, cfg: EvolutionConfig):
        self.cfg = cfg
        self.loader = SkillLoader(cfg)
        self.skills_root = Path(cfg.skills_path)
        self.backup_dir = self.skills_root / ".curator_backups"
        self.state_file = AGENT_HOME / "memory" / "curator_state.json"

    def should_run(self, is_game_in_progress: bool) -> bool:
        """判断是否应该触发 Curator。"""
        if is_game_in_progress:
            return False
        if not self.cfg.curator.enabled:
            return False

        state = self._load_state()
        last_run = state.get("last_run_at")
        if not last_run:
            self._save_state({"last_run_at": datetime.now(timezone.utc).isoformat()})
            return False

        hours_since = (datetime.now(timezone.utc) - datetime.fromisoformat(last_run)).total_seconds() / 3600
        return hours_since >= self.cfg.curator.interval_hours

    def run(self) -> Dict:
        """执行 Curator 审查。返回操作摘要。"""
        self._snapshot()

        summary = {"phase1": {}, "phase2": {}}

        summary["phase1"] = self._phase1_state_transitions()
        summary["phase2"] = self._phase2_llm_review()

        self._save_state({"last_run_at": datetime.now(timezone.utc).isoformat()})

        return summary

    def _phase1_state_transitions(self) -> Dict:
        """确定性状态转移：active → stale → archived。"""
        result = {"staled": [], "archived": []}
        cfg = self.cfg.versioning

        for skill_dir in self._iter_skill_dirs():
            meta_path = skill_dir / ".versions.json"
            if not meta_path.exists():
                continue

            with open(meta_path) as f:
                meta = json.load(f)

            for v_name, v_data in meta.get("versions", {}).items():
                if v_data.get("pinned"):
                    continue

                last_used = v_data.get("usage", {}).get("last_used") or v_data.get("created_at", "")
                if not last_used:
                    continue

                days_since = (datetime.now(timezone.utc) - datetime.fromisoformat(last_used)).days

                if v_data.get("status") == "active" and days_since >= cfg.demotion_stale_days:
                    v_data["status"] = "stale"
                    result["staled"].append(f"{skill_dir.name}/{v_name}")

                elif v_data.get("status") == "stale" and days_since >= cfg.demotion_archive_days:
                    v_data["status"] = "archived"
                    result["archived"].append(f"{skill_dir.name}/{v_name}")

            with open(meta_path, "w") as f:
                json.dump(meta, f, indent=2, ensure_ascii=False)

        return result

    def _phase2_llm_review(self) -> Dict:
        """LLM 审查：对每个非 pinned、非 bundled 策略做 keep/patch/consolidate/archive 判定。"""
        result = {"reviewed": 0, "kept": 0, "patched": 0, "consolidated": 0, "archived": 0}

        review_llm = LLMCaller()
        review_llm.model = self.cfg.clustering_model

        skills_reviewed = 0
        for skill_dir in self._iter_skill_dirs():
            if skills_reviewed >= self.cfg.curator.max_iterations:
                break

            meta_path = skill_dir / ".versions.json"
            if not meta_path.exists():
                continue

            with open(meta_path) as f:
                meta = json.load(f)

            current_v = meta.get("current_default", "v1")
            v_data = meta.get("versions", {}).get(current_v, {})

            if v_data.get("pinned") or v_data.get("source") == "bundled":
                continue

            v_file = skill_dir / f"{current_v}.md"
            if not v_file.exists():
                continue

            with open(v_file) as f:
                content = f.read()

            usage = v_data.get("usage", {})
            decision = self._llm_review_skill(content, usage, review_llm)

            result["reviewed"] += 1
            skills_reviewed += 1

            if decision == "keep":
                result["kept"] += 1
            elif decision == "archive":
                v_data["status"] = "archived"
                result["archived"] += 1
                with open(meta_path, "w") as f:
                    json.dump(meta, f, indent=2, ensure_ascii=False)

        return result

    def _llm_review_skill(self, content: str, usage: Dict, llm: LLMCaller) -> str:
        """LLM 审查单个策略，返回 keep/patch/consolidate/archive。"""
        prompt = f"""审查以下狼人杀策略文档，做出维护决策。

策略内容：
{content[:3000]}

使用数据：
- 对局数: {usage.get('games_played', 0)}
- 胜率: {usage.get('win_rate', 0):.2f}
- 最后使用: {usage.get('last_used', 'N/A')}

判定标准：
- keep: 策略质量良好，数据支持
- patch: 有小瑕疵需要修补
- consolidate: 与其他策略重叠，应合并
- archive: 质量不足或完全被新版本替代

只回答 keep / patch / consolidate / archive 之一。"""

        try:
            resp = llm.client.chat.completions.create(
                model=llm.model,
                messages=[
                    {"role": "system", "content": "You are a strategy library curator."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                max_tokens=20,
            )
            answer = (resp.choices[0].message.content or "").strip().lower()
            for decision in ["keep", "patch", "consolidate", "archive"]:
                if decision in answer:
                    return decision
        except Exception:
            pass
        return "keep"

    def _snapshot(self):
        """创建技能库快照。"""
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        snapshot_dir = self.backup_dir / ts
        snapshot_dir.mkdir(parents=True, exist_ok=True)

        tar_path = snapshot_dir / "skills.tar.gz"
        with tarfile.open(str(tar_path), "w:gz") as tar:
            tar.add(str(self.skills_root), arcname="skills")

        snapshots = sorted(self.backup_dir.iterdir())
        while len(snapshots) > 5:
            oldest = snapshots.pop(0)
            shutil.rmtree(oldest)

    def _iter_skill_dirs(self):
        """遍历所有策略目录。"""
        for role_dir in self.skills_root.iterdir():
            if not role_dir.is_dir() or role_dir.name.startswith("."):
                continue
            for skill_dir in role_dir.iterdir():
                if skill_dir.is_dir():
                    yield skill_dir

    def _load_state(self) -> Dict:
        if self.state_file.exists():
            with open(self.state_file) as f:
                return json.load(f)
        return {}

    def _save_state(self, state: Dict):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_file, "w") as f:
            json.dump(state, f)
