"""evolution/skill_loader.py — 渐进式策略加载器

三层加载：
  Layer 1 (始终): 策略名+描述索引，~60 tokens/策略
  Layer 2 (对局开始时): 当前角色+当前阶段相关的 1-3 个策略全文
  Layer 3 (反思时): 按需加载非默认版本，用于对比
"""
import json
import random
from pathlib import Path
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone

from evolution.config import EvolutionConfig


class SkillLoader:
    def __init__(self, cfg: EvolutionConfig):
        self.cfg = cfg
        self.skills_root = Path(cfg.skills_path)
        self._index_cache: Optional[Dict] = None

    # ── Layer 1: 索引 ──────────────────────────────────────

    def load_index(self) -> List[Dict[str, str]]:
        """加载全局策略索引（第 1 层），始终注入 system prompt。"""
        index_path = self.skills_root / ".skill_index.json"
        if not index_path.exists():
            return []
        with open(index_path) as f:
            data = json.load(f)
        return data.get("skills", [])

    def format_index_for_prompt(self, my_role: str) -> str:
        """将索引格式化为 prompt 片段。只展示当前角色 + common 类别。"""
        skills = self.load_index()
        relevant = [s for s in skills if s.get("role") in (my_role, "common")]
        if not relevant:
            return ""
        lines = ["## Available Strategy Skills"]
        for s in relevant:
            lines.append(f"- **{s['name']}** (v{s['current_version']}): {s['description']}")
        return "\n".join(lines)

    # ── Layer 2: 全文加载 ────────────────────────────────────

    def load_skill_full(self, skill_name: str, version: Optional[str] = None) -> Optional[str]:
        """加载指定策略的完整 Markdown 内容（第 2 层）。"""
        versions_meta = self._load_versions_meta(skill_name)
        if not versions_meta:
            return None

        if version is None:
            version = versions_meta.get("current_default", "v1")

        skill_dir = self._find_skill_dir(skill_name)
        if not skill_dir:
            return None

        file_path = skill_dir / f"{version}.md"
        if not file_path.exists():
            return None

        with open(file_path) as f:
            return f.read()

    def load_skills_for_context(self, my_role: str, phase: str) -> str:
        """根据当前角色和阶段，加载相关策略全文。最多加载 3 个。"""
        skills = self.load_index()
        relevant = [
            s for s in skills
            if s.get("role") in (my_role, "common")
        ]

        phase_tags = self._phase_to_tags(phase)
        scored = []
        for s in relevant:
            overlap = len(set(s.get("tags", [])) & phase_tags)
            scored.append((overlap, s))
        scored.sort(key=lambda x: -x[0])

        loaded_parts = []
        for _, s in scored[:3]:
            content = self.load_skill_full(s["name"])
            if content:
                loaded_parts.append(f"### Strategy: {s['name']}\n{content}")

        return "\n\n".join(loaded_parts)

    # ── 版本元数据 ─────────────────────────────────────────

    def _load_versions_meta(self, skill_name: str) -> Optional[Dict]:
        """加载某策略的 .versions.json。"""
        skill_dir = self._find_skill_dir(skill_name)
        if not skill_dir:
            return None
        meta_path = skill_dir / ".versions.json"
        if not meta_path.exists():
            return None
        with open(meta_path) as f:
            return json.load(f)

    def _find_skill_dir(self, skill_name: str) -> Optional[Path]:
        """根据策略名在目录树中定位其所在目录。"""
        parts = skill_name.split("-", 1)
        if len(parts) == 2:
            role_dir, topic = parts
            candidate = self.skills_root / role_dir / topic
            if candidate.is_dir():
                return candidate

        for subdir in ["common", "seer", "wolf", "witch", "guard", "hunter"]:
            candidate = self.skills_root / subdir / skill_name
            if candidate.is_dir():
                return candidate
            if len(parts) == 2:
                candidate = self.skills_root / subdir / parts[1]
                if candidate.is_dir():
                    return candidate

        for d in self.skills_root.rglob(skill_name):
            if d.is_dir():
                return d
        return None

    def _phase_to_tags(self, phase: str) -> set:
        """将游戏阶段映射为策略标签集合。"""
        PHASE_TAG_MAP = {
            "seer_check": {"check", "night", "verify"},
            "wolf_kill": {"kill", "night", "wolf-strategy"},
            "witch_action": {"potion", "night", "heal", "poison"},
            "guard_action": {"protect", "night", "guard"},
            "election": {"sheriff", "election", "campaign"},
            "discussion": {"speech", "analysis", "bluff"},
            "vote": {"vote", "elimination", "strategy"},
        }
        return PHASE_TAG_MAP.get(phase, {"general"})

    # ── 版本竞争相关 ─────────────────────────────────────────

    def get_version_for_game(self, skill_name: str) -> str:
        """版本竞争：决定本局使用哪个版本。"""
        meta = self._load_versions_meta(skill_name)
        if not meta:
            return "v1"

        current = meta.get("current_default", "v1")
        versions = meta.get("versions", {})

        for v_name, v_data in versions.items():
            if v_data.get("status") == "candidate":
                usage = v_data.get("usage", {})
                games = usage.get("games_played", 0)
                if games < self.cfg.versioning.warmup_games:
                    if random.random() < self.cfg.versioning.warmup_allocation:
                        return v_name

        return current

    def record_version_usage(self, skill_name: str, version: str, won: bool):
        """对局结束后记录版本使用情况。"""
        meta = self._load_versions_meta(skill_name)
        if not meta or version not in meta.get("versions", {}):
            return

        v = meta["versions"][version]
        usage = v.setdefault("usage", {"games_played": 0, "wins": 0, "win_rate": 0})
        usage["games_played"] += 1
        if won:
            usage["wins"] += 1
        usage["win_rate"] = usage["wins"] / usage["games_played"]

        if v.get("status") == "candidate":
            self._check_promotion(meta, skill_name, version)

        self._save_versions_meta(skill_name, meta)

    def _check_promotion(self, meta: Dict, skill_name: str, candidate_version: str):
        """检查 candidate 版本是否满足升级条件。"""
        candidate = meta["versions"][candidate_version]
        current_default = meta.get("current_default", "v1")
        current = meta["versions"].get(current_default, {})

        c_games = candidate.get("usage", {}).get("games_played", 0)
        c_wr = candidate.get("usage", {}).get("win_rate", 0)
        d_wr = current.get("usage", {}).get("win_rate", 0)

        cfg = self.cfg.versioning
        if (c_games >= cfg.promotion_min_games and
                c_wr - d_wr >= cfg.promotion_min_win_rate_delta):
            candidate["status"] = "active"
            meta["current_default"] = candidate_version
            if current:
                current["status"] = "superseded"

    def _save_versions_meta(self, skill_name: str, meta: Dict):
        skill_dir = self._find_skill_dir(skill_name)
        if skill_dir:
            with open(skill_dir / ".versions.json", "w") as f:
                json.dump(meta, f, indent=2, ensure_ascii=False)

    def create_new_version(self, skill_name: str, content: str,
                           source: str = "debounced_update",
                           trigger_cluster: str = "") -> str:
        """创建策略新版本，返回版本号（如 "v3"）。"""
        skill_dir = self._find_skill_dir(skill_name)
        if not skill_dir:
            role, topic = skill_name.split("-", 1) if "-" in skill_name else ("common", skill_name)
            skill_dir = self.skills_root / role / topic
            skill_dir.mkdir(parents=True, exist_ok=True)
            meta = {"skill_name": skill_name, "current_default": "v0", "versions": {}}
            version_num = 1
        else:
            meta = self._load_versions_meta(skill_name) or {
                "skill_name": skill_name, "current_default": "v1", "versions": {}
            }
            existing_versions = meta.get("versions", {})
            version_num = max(
                (int(v.replace("v", "")) for v in existing_versions.keys() if v.startswith("v")),
                default=0
            ) + 1

        version_name = f"v{version_num}"

        with open(skill_dir / f"{version_name}.md", "w") as f:
            f.write(content)

        meta["versions"][version_name] = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "trigger_cluster": trigger_cluster,
            "pinned": False,
            "status": "candidate",
            "usage": {"games_played": 0, "wins": 0, "win_rate": 0, "last_used": None}
        }

        max_v = self.cfg.versioning.max_versions_per_skill
        if len(meta["versions"]) > max_v:
            self._prune_old_versions(meta, skill_dir, max_v)

        self._save_versions_meta(skill_name, meta)
        self._rebuild_index()

        return version_name

    def _prune_old_versions(self, meta: Dict, skill_dir: Path, keep: int):
        """删除最旧的非 pinned 版本，保留 keep 个。"""
        versions = meta["versions"]
        sortable = [
            (v_name, v_data) for v_name, v_data in versions.items()
            if not v_data.get("pinned") and v_data.get("status") in ("superseded", "archived")
        ]
        sortable.sort(key=lambda x: x[1].get("created_at", ""))

        while len(versions) > keep and sortable:
            v_name, _ = sortable.pop(0)
            (skill_dir / f"{v_name}.md").unlink(missing_ok=True)
            del versions[v_name]

    def _rebuild_index(self):
        """扫描 skills/ 目录，重建 .skill_index.json。"""
        skills = []
        for role_dir in self.skills_root.iterdir():
            if not role_dir.is_dir() or role_dir.name.startswith("."):
                continue
            for skill_dir in role_dir.iterdir():
                if not skill_dir.is_dir():
                    continue
                meta_path = skill_dir / ".versions.json"
                if not meta_path.exists():
                    continue
                with open(meta_path) as f:
                    meta = json.load(f)
                current_v = meta.get("current_default", "v1")
                desc = self._extract_description(skill_dir, current_v)
                tags = self._extract_tags(skill_dir, current_v)
                skills.append({
                    "name": meta.get("skill_name", skill_dir.name),
                    "description": desc,
                    "role": role_dir.name,
                    "current_version": current_v.replace("v", ""),
                    "tags": tags,
                })

        index_path = self.skills_root / ".skill_index.json"
        with open(index_path, "w") as f:
            json.dump({
                "skills": skills,
                "updated_at": datetime.now(timezone.utc).isoformat()
            }, f, indent=2, ensure_ascii=False)

    def _extract_description(self, skill_dir: Path, version: str) -> str:
        """从 frontmatter 提取 description。"""
        vfile = skill_dir / f"{version}.md"
        if not vfile.exists():
            return ""
        content = vfile.read_text()
        if content.startswith("---"):
            end = content.find("---", 3)
            if end > 0:
                fm = content[3:end]
                for line in fm.split("\n"):
                    if line.strip().startswith("description:"):
                        return line.split(":", 1)[1].strip()
        return ""

    def _extract_tags(self, skill_dir: Path, version: str) -> List[str]:
        """从 frontmatter 提取 tags。"""
        vfile = skill_dir / f"{version}.md"
        if not vfile.exists():
            return []
        content = vfile.read_text()
        if content.startswith("---"):
            end = content.find("---", 3)
            if end > 0:
                fm = content[3:end]
                for line in fm.split("\n"):
                    if line.strip().startswith("tags:"):
                        raw = line.split(":", 1)[1].strip().strip("[]")
                        return [t.strip() for t in raw.split(",") if t.strip()]
        return []

    # ── 回退 ─────────────────────────────────────────────────

    def rollback(self, skill_name: str, target_version: str) -> bool:
        """回退策略到指定版本。"""
        meta = self._load_versions_meta(skill_name)
        if not meta or target_version not in meta.get("versions", {}):
            return False

        old_default = meta.get("current_default")
        if old_default and old_default in meta["versions"]:
            meta["versions"][old_default]["status"] = "superseded"

        meta["current_default"] = target_version
        meta["versions"][target_version]["status"] = "active"
        self._save_versions_meta(skill_name, meta)
        self._rebuild_index()
        return True
