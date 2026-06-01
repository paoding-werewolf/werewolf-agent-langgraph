# 防抖策略自进化机制 — LangGraph Agent 侧 Spec Coding 手册

> 基于文档: `debounced-policy-update-design.md` + `hermes-agent-self-evolution-analysis.md`
> 目标仓库: `werewolf-agent-langgraph/`
> 撰写时间: 2026-05-31
> 执行前提: 读完本手册 + 仓库中 `app/` 目录全部源码

---

## 〇、总览：要在现有 Agent 里嵌入什么

现有 Agent 是 `perceive → reflect → act` 三节点决策图，策略是写死在 `prompt_storage.py` 中的静态文本。本 spec 的目标是嵌入一套 **运行时自进化管道**，使策略文档能在多局对局中自动积累、防抖过滤、版本化迭代。

### 嵌入后的 Agent 生命周期

```
对局中（每个决策点）
  perceive → reflect → act                ← 现有流程，不改
  + 即时标记检测（在 reflect 中附加）       ← 新增

对局结束
  /agent/reflect (POST)                    ← 新增 API
  → 深度反思引擎（结构化因果链输出）
  → 写入缓冲池
  → 触发聚合检查
  → 满足确认条件 → 创建策略新版本

后台（定期）
  Curator 审查                              ← 新增后台任务
  → 策略库清理/合并/归档
```

### 新增模块清单

| 模块 | 文件路径 | 职责 |
|------|----------|------|
| 配置中心 | `app/evolution/config.py` | YAML 配置加载，所有阈值集中管理 |
| 反思引擎 | `app/evolution/reflection_engine.py` | 对局结束后深度反思，产出结构化建议 |
| 缓冲池 | `app/evolution/buffer_pool.py` | 建议存储、过期清理 |
| 语义聚类 | `app/evolution/clustering.py` | 同场景建议聚合 |
| 确认判定 | `app/evolution/confirmation.py` | 双重判定（频率 + 因果强度） |
| 版本管理 | `app/evolution/version_manager.py` | 策略版本化、竞争、回退 |
| 策略加载器 | `app/evolution/skill_loader.py` | 渐进式策略加载，替代硬编码 prompt |
| 即时标记 | `app/evolution/in_game_flagger.py` | 对局中策略矛盾检测与标记 |
| Curator | `app/evolution/curator.py` | 策略库定期维护 |
| 记忆系统 | `app/memory/` | 四层记忆（工作记忆/对手建模/自我画像/对局历史） |
| API 路由 | `app/main_ws.py` (扩展) | 新增 reflect/status/rollback 端点 |

---

## 一、配置中心

### 文件: `app/evolution/config.py`

所有阈值集中配置，不硬编码。从 `~/.werewolf-agent/config.yaml` 加载，支持环境变量覆盖。

```python
"""evolution/config.py — 集中配置管理"""
import os
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

AGENT_HOME = Path(os.getenv("WEREWOLF_AGENT_HOME", "~/.werewolf-agent")).expanduser()

@dataclass
class ReflectionConfig:
    causal_analysis_enabled: bool = True
    confidence_calibration: bool = True

@dataclass
class BufferConfig:
    path: str = str(AGENT_HOME / "policy_buffer")
    max_age_days: int = 30
    max_cluster_size: int = 20
    cleanup_interval_hours: int = 24
    semantic_similarity_threshold: float = 0.75

@dataclass
class ConfirmationConfig:
    # 普通通道
    normal_min_count: int = 3
    normal_min_consistency_rate: float = 0.60
    normal_min_avg_causal_strength: float = 0.50
    # 快速通道（高因果强度）
    fast_track_min_causal_strength: float = 0.80
    fast_track_min_count: int = 2

@dataclass
class VersioningConfig:
    warmup_games: int = 5
    warmup_allocation: float = 0.5
    promotion_min_games: int = 5
    promotion_min_win_rate_delta: float = 0.10
    demotion_stale_days: int = 14
    demotion_archive_days: int = 30
    max_versions_per_skill: int = 5

@dataclass
class CuratorConfig:
    enabled: bool = True
    interval_hours: int = 168  # 7 天
    min_idle_hours: int = 2
    max_iterations: int = 8

@dataclass
class EvolutionConfig:
    enabled: bool = True
    reflection: ReflectionConfig = field(default_factory=ReflectionConfig)
    buffer: BufferConfig = field(default_factory=BufferConfig)
    confirmation: ConfirmationConfig = field(default_factory=ConfirmationConfig)
    versioning: VersioningConfig = field(default_factory=VersioningConfig)
    curator: CuratorConfig = field(default_factory=CuratorConfig)
    clustering_model: str = "deepseek-chat"  # 轻量模型，用于语义聚类
    reflection_model: str = ""  # 空=使用主模型; 可配置为轻量模型
    in_game_flag_causal_multiplier: float = 1.3
    skills_path: str = str(AGENT_HOME / "skills")

def load_config() -> EvolutionConfig:
    """从 YAML 文件加载配置，不存在则返回默认值。"""
    config_path = AGENT_HOME / "config.yaml"
    if config_path.exists():
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}
        dp = raw.get("debounced_policy", {})
        # 逐层合并，YAML 中缺省的字段使用 dataclass 默认值
        cfg = EvolutionConfig()
        _merge_dataclass(cfg.reflection, dp.get("reflection", {}))
        _merge_dataclass(cfg.buffer, dp.get("buffer", {}))
        _merge_dataclass(cfg.confirmation, dp.get("confirmation", {}).get("normal", {}))
        # ... 其他字段同理
        return cfg
    return EvolutionConfig()

def _merge_dataclass(obj, overrides: dict):
    """将 dict 中的键值对覆盖到 dataclass 实例上。"""
    for k, v in overrides.items():
        if hasattr(obj, k):
            setattr(obj, k, v)
```

**初始化目录结构：**

```python
def ensure_directories(cfg: EvolutionConfig):
    """启动时调用，创建所有必要目录。"""
    dirs = [
        Path(cfg.buffer.path) / "pending",
        Path(cfg.buffer.path) / "clusters",
        Path(cfg.buffer.path) / "confirmed",
        Path(cfg.buffer.path) / "expired",
        Path(cfg.skills_path),
        AGENT_HOME / "memory" / "opponents",
        AGENT_HOME / "memory" / "self_model",
        AGENT_HOME / "memory" / "game_archive",
        AGENT_HOME / "skills" / ".curator_backups",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
```

---

## 二、策略库（Skill Library）

### 2.1 目录结构

```
~/.werewolf-agent/skills/
├── seer/
│   ├── identity-timing/
│   │   ├── v1.md
│   │   ├── v2.md
│   │   └── .versions.json
│   ├── check-priority/
│   │   ├── v1.md
│   │   └── .versions.json
│   └── ...
├── wolf/
│   ├── bluff-strategy/
│   │   └── ...
│   └── kill-priority/
│       └── ...
├── witch/
│   └── ...
├── guard/
│   └── ...
├── common/
│   ├── voting-strategy/
│   └── speech-craft/
└── .skill_index.json        # 全局索引（渐进式加载第 1 层）
```

### 2.2 策略文件格式

每个策略文件是 Markdown + YAML frontmatter，兼容 agentskills.io 标准：

```yaml
---
name: seer-identity-timing
description: 预言家何时跳身份的策略指导
version: 2
role: seer
tags: [identity, timing, jump]
source: debounced_update  # bundled | debounced_update | gepa_evolution | manual
trigger_cluster: cluster_seer-identity-timing_seer-alive-1
---

## When to Use
当你是预言家，需要决定是否在白天公开身份时使用此策略。

## Procedure
1. 评估当前存活轮次和已查验信息量
2. 若仅存活 1 轮且查验信息 ≤ 1 人 → 延迟跳身份
3. 若已查验 2+ 人且包含狼人 → 考虑跳身份传递信息
4. 若有对跳预言家 → 用查验信息自证

## Pitfalls
- 第 1 天跳身份且无保护（守卫/女巫）→ 高概率被刀
- 不要为了跳身份而跳，确保传递的信息有价值

## Verification
- 跳身份后是否成功传递了关键查验信息
- 是否在被刀前完成了信息传递
```

### 2.3 `.versions.json` 元数据格式

每个策略目录下维护一个 `.versions.json`：

```json
{
  "skill_name": "seer-identity-timing",
  "current_default": "v2",
  "versions": {
    "v1": {
      "created_at": "2026-05-20T00:00:00Z",
      "source": "bundled",
      "pinned": true,
      "status": "superseded",
      "usage": {
        "games_played": 8,
        "wins": 3,
        "win_rate": 0.375,
        "last_used": "2026-05-28T00:00:00Z"
      }
    },
    "v2": {
      "created_at": "2026-05-30T12:00:00Z",
      "source": "debounced_update",
      "trigger_cluster": "cluster_seer-identity-timing_seer-alive-1",
      "pinned": false,
      "status": "active",
      "usage": {
        "games_played": 12,
        "wins": 7,
        "win_rate": 0.583,
        "last_used": "2026-05-31T00:00:00Z"
      }
    }
  }
}
```

### 2.4 `.skill_index.json` 全局索引

渐进式加载第 1 层——始终加载，约 60 tokens/策略：

```json
{
  "skills": [
    {
      "name": "seer-identity-timing",
      "description": "预言家何时跳身份的策略指导",
      "role": "seer",
      "current_version": "v2",
      "tags": ["identity", "timing"]
    },
    {
      "name": "wolf-bluff-strategy",
      "description": "狼人悍跳神职的策略",
      "role": "wolf",
      "current_version": "v1",
      "tags": ["bluff", "impersonate"]
    }
  ],
  "updated_at": "2026-05-31T00:00:00Z"
}
```

---

## 三、策略加载器 (`skill_loader.py`)

替代现有 `prompt_storage.py` 中的硬编码策略，实现渐进式加载。

### 文件: `app/evolution/skill_loader.py`

```python
"""evolution/skill_loader.py — 渐进式策略加载器

三层加载：
  Layer 1 (始终): 策略名+描述索引，~60 tokens/策略
  Layer 2 (对局开始时): 当前角色+当前阶段相关的 1-3 个策略全文
  Layer 3 (反思时): 按需加载非默认版本，用于对比
"""
import json
from pathlib import Path
from typing import List, Optional, Dict, Any

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
        """将索引格式化为 prompt 片段。
        
        只展示当前角色 + common 类别的策略，减少 token 消耗。
        """
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
        """加载指定策略的完整 Markdown 内容（第 2 层）。
        
        Args:
            skill_name: 策略名（如 "seer-identity-timing"）
            version: 版本号（如 "v2"），None 则使用 current_default
        """
        versions_meta = self._load_versions_meta(skill_name)
        if not versions_meta:
            return None
        
        if version is None:
            version = versions_meta.get("current_default", "v1")
        
        # 从 skill_name 解析目录路径：
        # "seer-identity-timing" → 查找 skills/seer/identity-timing/ 或 skills/common/identity-timing/
        skill_dir = self._find_skill_dir(skill_name)
        if not skill_dir:
            return None
        
        file_path = skill_dir / f"{version}.md"
        if not file_path.exists():
            return None
        
        with open(file_path) as f:
            return f.read()

    def load_skills_for_context(self, my_role: str, phase: str) -> str:
        """根据当前角色和阶段，加载相关策略全文。
        
        匹配规则：
          1. 策略的 role == my_role 或 role == "common"
          2. 策略的 tags 与当前 phase 有交集
        
        最多加载 3 个策略，避免 prompt 过长。
        """
        skills = self.load_index()
        relevant = [
            s for s in skills
            if s.get("role") in (my_role, "common")
        ]
        
        # 按阶段相关度排序（简单实现：tag 匹配数量）
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
        """根据策略名在目录树中定位其所在目录。
        
        策略名格式: "{role}-{topic}" 或 "{topic}"
        搜索顺序:
          1. skills/{role}/{topic}/
          2. skills/common/{topic}/
          3. 遍历 skills/ 下所有子目录
        """
        parts = skill_name.split("-", 1)
        if len(parts) == 2:
            role_dir, topic = parts
            candidate = self.skills_root / role_dir / topic
            if candidate.is_dir():
                return candidate
        
        # fallback: 搜索 common
        for subdir in ["common", "seer", "wolf", "witch", "guard", "hunter"]:
            candidate = self.skills_root / subdir / skill_name
            if candidate.is_dir():
                return candidate
            # 尝试 topic 部分
            if len(parts) == 2:
                candidate = self.skills_root / subdir / parts[1]
                if candidate.is_dir():
                    return candidate
        
        # 最后遍历
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
        """版本竞争：决定本局使用哪个版本。
        
        warmup 期间按 warmup_allocation 比例随机分配。
        warmup 后使用 current_default。
        """
        import random
        meta = self._load_versions_meta(skill_name)
        if not meta:
            return "v1"
        
        current = meta.get("current_default", "v1")
        versions = meta.get("versions", {})
        
        # 查找处于 warmup 的 candidate 版本
        for v_name, v_data in versions.items():
            if v_data.get("status") == "candidate":
                usage = v_data.get("usage", {})
                games = usage.get("games_played", 0)
                if games < self.cfg.versioning.warmup_games:
                    # warmup 期间按比例随机
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
        
        # 检查是否可以升级 candidate → active
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
            # 升级
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
        """创建策略新版本，返回版本号（如 "v3"）。
        
        不修改现有版本，新文件为 v(n+1).md。
        """
        skill_dir = self._find_skill_dir(skill_name)
        if not skill_dir:
            # 策略不存在，创建新策略
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
        
        # 写入策略文件
        with open(skill_dir / f"{version_name}.md", "w") as f:
            f.write(content)
        
        # 更新元数据
        from datetime import datetime, timezone
        meta["versions"][version_name] = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "trigger_cluster": trigger_cluster,
            "pinned": False,
            "status": "candidate",  # 新版本先进入 candidate，通过版本竞争上位
            "usage": {"games_played": 0, "wins": 0, "win_rate": 0, "last_used": None}
        }
        
        # 限制版本数
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
                # 读取 v1.md 或当前版本的 frontmatter 获取 description
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
        from datetime import datetime, timezone
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
        # 简单解析 YAML frontmatter
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
                        # 解析 [tag1, tag2] 格式
                        raw = line.split(":", 1)[1].strip().strip("[]")
                        return [t.strip() for t in raw.split(",") if t.strip()]
        return []

    # ── 回退 ─────────────────────────────────────────────────

    def rollback(self, skill_name: str, target_version: str) -> bool:
        """回退策略到指定版本。"""
        meta = self._load_versions_meta(skill_name)
        if not meta or target_version not in meta.get("versions", {}):
            return False
        
        # 当前默认标记为 superseded
        old_default = meta.get("current_default")
        if old_default and old_default in meta["versions"]:
            meta["versions"][old_default]["status"] = "superseded"
        
        meta["current_default"] = target_version
        meta["versions"][target_version]["status"] = "active"
        self._save_versions_meta(skill_name, meta)
        self._rebuild_index()
        return True
```

---

## 四、反思引擎 (`reflection_engine.py`)

### 文件: `app/evolution/reflection_engine.py`

核心模块。对局结束后被 `/agent/reflect` API 调用，产出结构化因果链 + 策略建议。

```python
"""evolution/reflection_engine.py — 结构化反思引擎

输入：完整 game trace + 即时标记 + 工作记忆
输出：ReflectionResult（因果链 + 策略建议 + 场景标签 + 置信度）
"""
import json
import re
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone

from agents.llm_caller import llm, LLMCaller
from evolution.config import EvolutionConfig


# ── 数据结构 ────────────────────────────────────────────────

@dataclass
class CausalStep:
    action: str                     # "第1天跳了预言家身份"
    intermediate: str               # "狼人确认了我的位置"
    outcome: str                    # "第2天被狼刀"
    is_strategy_driven: bool = True
    is_luck_driven: bool = False

@dataclass
class SceneTags:
    """场景标签，用于缓冲池分组聚合。"""
    role: str = ""
    role_survived_rounds: int = 0
    sheriff_contested: bool = False
    first_night_target: str = ""    # "seer" / "witch" / "villager" / "none"
    wolf_aggression: str = "medium" # high / medium / low
    good_coordination: str = "medium"
    critical_phase: str = ""        # first_day_speech / first_vote / mid_game / end_game
    result: str = ""                # won / lost
    death_cause: str = ""           # wolf_kill / vote_out / poison / shoot / guard_paradox

@dataclass
class StrategySuggestion:
    text: str                        # 策略建议文本
    confidence: float = 0.5          # 自评置信度 (0-1)
    direction: str = "modify"        # create / modify / discard
    target_skill: str = ""           # 目标策略名
    match_level: str = "high"        # high / medium / low (strategy_gap)
    causal_strength: float = 0.5     # 因果强度 (0-1)

@dataclass
class ReflectionResult:
    suggestion_id: str
    game_id: str
    my_role: str
    result: str                      # won / lost
    scene_tags: SceneTags = field(default_factory=SceneTags)
    causal_chain: List[CausalStep] = field(default_factory=list)
    suggestion: StrategySuggestion = field(default_factory=StrategySuggestion)
    in_game_flags: List[Dict] = field(default_factory=list)  # 对局中的即时标记


# ── 反思 Prompt ─────────────────────────────────────────────

REFLECT_SYSTEM_PROMPT = """你是一个狼人杀策略分析专家。你的任务是回顾一局完整的对局记录，
进行深度因果分析，并产出结构化的策略建议。

你必须严格区分：
- 策略导致的后果（因为某个决策引发了可追踪的因果链）
- 运气导致的后果（随机事件、对手不可预测的行为）

输出必须是合法的 YAML 格式，不要包含多余文本。"""

REFLECT_USER_TEMPLATE = """## 对局信息
- 房间: {game_id}
- 我的角色: {my_role}
- 我的座位: {my_seat}
- 结果: {result}

## 完整对局 Trace
{game_trace}

## 对局中的即时标记（如果有）
{in_game_flags}

## 当前使用的策略
{current_strategies}

## 请输出以下 YAML 结构

```yaml
scene_tags:
  role: {my_role}
  role_survived_rounds: <我存活了几轮，整数>
  sheriff_contested: <是否有警长对跳，true/false>
  first_night_target: <首夜被刀者角色，如 "seer"/"villager"/"none">
  wolf_aggression: <high/medium/low>
  good_coordination: <high/medium/low>
  critical_phase: <first_day_speech/first_vote/mid_game/end_game>
  result: <won/lost>
  death_cause: <wolf_kill/vote_out/poison/shoot/guard_paradox/none>

causal_chain:
  - action: "<我做了什么关键决策>"
    intermediate: "<导致了什么中间结果>"
    outcome: "<最终产生了什么后果>"
    is_strategy_driven: <true/false>
    is_luck_driven: <true/false>
  # 可以有多条，列出所有关键因果链

suggestion:
  text: "<一句话策略建议>"
  confidence: <0-1，你自评这条建议有多可靠>
  direction: <create/modify/discard>
  target_skill: "<目标策略名，如 seer-identity-timing>"
  match_level: <high/medium/low>
  # high = 策略明确覆盖当前场景
  # medium = 策略部分覆盖，但局势有差异
  # low = 策略无覆盖，完全是独立判断 (strategy_gap)

causal_strength: <0-1，本局中该建议与结果之间的因果关联强度>
# 低 = 大概率是运气决定的
# 高 = 大概率是策略因果
```

注意事项：
1. causal_strength 和 confidence 是两个独立维度：
   - confidence: 你觉得这条建议有多正确（主观）
   - causal_strength: 本局的输赢与你的策略选择有多大因果关系（客观）
2. 如果这局输赢主要靠运气，causal_strength 应该低
3. match_level = low 的建议不会进入策略更新管道，只标记为 strategy_gap
4. 如果没什么值得修改的，direction 填 discard"""


class ReflectionEngine:
    def __init__(self, cfg: EvolutionConfig, llm_caller: Optional[LLMCaller] = None):
        self.cfg = cfg
        self.llm = llm_caller or llm
        # 可选使用单独的模型做反思
        if cfg.reflection_model:
            self.reflect_llm = LLMCaller()
            self.reflect_llm.model = cfg.reflection_model
        else:
            self.reflect_llm = self.llm

    def reflect(self,
                game_id: str,
                my_role: str,
                my_seat: str,
                result: str,
                game_trace: str,
                in_game_flags: List[Dict],
                current_strategies: str = "") -> Optional[ReflectionResult]:
        """执行对局后反思。
        
        Args:
            game_id: 房间 ID
            my_role: 我的角色
            my_seat: 我的座位号
            result: "won" 或 "lost"
            game_trace: 格式化的对局 trace 文本
            in_game_flags: 对局中产生的即时标记列表
            current_strategies: 本局使用的策略文本（供对比）
            
        Returns:
            ReflectionResult 或 None（解析失败时）
        """
        flags_text = json.dumps(in_game_flags, ensure_ascii=False, indent=2) if in_game_flags else "（无即时标记）"
        
        user_msg = REFLECT_USER_TEMPLATE.format(
            game_id=game_id,
            my_role=my_role,
            my_seat=my_seat,
            result=result,
            game_trace=game_trace,
            in_game_flags=flags_text,
            current_strategies=current_strategies or "（无策略文档）",
        )
        
        # 调用 LLM
        response = self._call_llm(user_msg)
        
        # 解析 YAML 输出
        parsed = self._parse_reflection_yaml(response)
        if not parsed:
            return None
        
        # 构建 ReflectionResult
        suggestion_id = f"sug_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{game_id}"
        
        scene = SceneTags(**{k: v for k, v in parsed.get("scene_tags", {}).items()
                            if k in SceneTags.__dataclass_fields__})
        
        causal_chain = []
        for step in parsed.get("causal_chain", []):
            causal_chain.append(CausalStep(**{k: v for k, v in step.items()
                                              if k in CausalStep.__dataclass_fields__}))
        
        sug_data = parsed.get("suggestion", {})
        suggestion = StrategySuggestion(**{k: v for k, v in sug_data.items()
                                           if k in StrategySuggestion.__dataclass_fields__})
        
        # 因果强度从顶层读取
        suggestion.causal_strength = float(parsed.get("causal_strength", 0.5))
        
        # 即时标记的因果强度加成
        if in_game_flags and suggestion.causal_strength > 0:
            suggestion.causal_strength = min(
                1.0,
                suggestion.causal_strength * self.cfg.in_game_flag_causal_multiplier
            )
        
        # match_level = medium 时因果强度打折
        if suggestion.match_level == "medium":
            suggestion.causal_strength *= 0.7
        
        return ReflectionResult(
            suggestion_id=suggestion_id,
            game_id=game_id,
            my_role=my_role,
            result=result,
            scene_tags=scene,
            causal_chain=causal_chain,
            suggestion=suggestion,
            in_game_flags=in_game_flags,
        )

    def _call_llm(self, user_msg: str) -> str:
        """调用反思用的 LLM（可能是独立模型）。"""
        # 直接调用 OpenAI client，不走 tool calling
        client = self.reflect_llm.client
        try:
            resp = client.chat.completions.create(
                model=self.reflect_llm.model,
                messages=[
                    {"role": "system", "content": REFLECT_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.3,  # 反思要低温度，更稳定
            )
            return resp.choices[0].message.content or ""
        except Exception as e:
            return f"ERROR: {e}"

    def _parse_reflection_yaml(self, text: str) -> Optional[Dict]:
        """从 LLM 输出中提取 YAML 并解析。"""
        import yaml
        
        # 尝试提取 ```yaml ... ``` 块
        match = re.search(r'```(?:yaml)?\s*\n(.*?)```', text, re.DOTALL)
        if match:
            yaml_text = match.group(1)
        else:
            yaml_text = text
        
        try:
            return yaml.safe_load(yaml_text)
        except yaml.YAMLError:
            return None


# ── Trace 格式化工具 ─────────────────────────────────────────

def format_game_trace(events: List[Dict], players: Dict) -> str:
    """将 AgentState 中的 events 列表格式化为可读的对局 trace。
    
    输出格式示例：
    --- Round 1 Night ---
    [guard_action] 守卫守护了 5 号
    [wolf_kill] 狼人刀了 7 号
    [seer_check] 3号(You) 查验了 4号 → 狼人
    
    --- Round 1 Day ---
    [election] 7号当选警长
    [discussion] 1号: "我怀疑3号..."
    [vote] 5号被投票出局 (6票)
    """
    lines = []
    current_round = 0
    
    for event in events:
        r = event.get("round", 1)
        status = event.get("status", "")
        content = event.get("content", "")
        traces = event.get("traces", [])
        
        if r != current_round:
            current_round = r
            lines.append(f"\n--- Round {r} ---")
        
        # 格式化 trace 动作
        trace_parts = []
        for t in traces:
            f = t.get("from", "?")
            to = t.get("to", "")
            act = t.get("action", "")
            trace_parts.append(f"{f}→{to}({act})" if to else f"{f}({act})")
        
        trace_str = " | " + ", ".join(trace_parts) if trace_parts else ""
        lines.append(f"[{status}] {content}{trace_str}")
    
    return "\n".join(lines)
```

---

## 五、缓冲池 (`buffer_pool.py`)

### 文件: `app/evolution/buffer_pool.py`

```python
"""evolution/buffer_pool.py — 策略建议缓冲池

职责：
  1. 接收反思引擎产出的建议
  2. 按场景标签路由到 pending/ 或 clusters/
  3. 管理建议生命周期（过期清理）
  
目录结构：
  policy_buffer/
  ├── pending/          单条待聚类建议
  ├── clusters/         聚合后的建议群
  ├── confirmed/        已确认（已写入版本库）
  └── expired/          过期丢弃
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
        """接收一条反思结果，写入缓冲池。
        
        返回处理状态：
          "buffered" — 写入 pending，等待聚类
          "clustered" — 归入已有 cluster
          "skipped" — match_level=low，不进入缓冲池
          "new_cluster" — 创建了新 cluster
        """
        sug = result.suggestion
        
        # match_level = low / strategy_gap → 不进入缓冲池
        if sug.match_level in ("low", "strategy_gap"):
            return "skipped"
        
        # 序列化为 YAML 文件
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
```

---

## 六、语义聚类 (`clustering.py`)

### 文件: `app/evolution/clustering.py`

```python
"""evolution/clustering.py — 建议语义聚类

将 pending/ 中的建议按场景标签分组，然后在组内做语义一致性检查。

聚类逻辑：
  1. 新建议到达 → 提取 scene_tags
  2. 在 clusters/ 中找匹配（场景标签交集 ≥ 阈值）
  3. 匹配到 → 加入 cluster，检查语义一致性
  4. 未匹配到 → 创建新 cluster
"""
import json
import yaml
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timezone

from evolution.config import EvolutionConfig
from agents.llm_caller import LLMCaller


class SuggestionClusterer:
    def __init__(self, cfg: EvolutionConfig):
        self.cfg = cfg
        self.buffer_root = Path(cfg.buffer.path)
        self.clusters_dir = self.buffer_root / "clusters"
        self.pending_dir = self.buffer_root / "pending"
        
        # 轻量 LLM 用于语义相似度判断
        self.cluster_llm = LLMCaller()
        if cfg.clustering_model:
            self.cluster_llm.model = cfg.clustering_model

    def process_pending(self) -> List[Dict]:
        """处理所有 pending 建议，归入或创建 cluster。
        
        返回被处理的建议列表（含处理结果）。
        """
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
            
            # 从 pending 中移除（已归入 cluster）
            pf.unlink()
        
        return processed

    def _assign_to_cluster(self, suggestion: Dict) -> Dict:
        """将一条建议分配到最匹配的 cluster，或创建新 cluster。"""
        scene_tags = suggestion.get("scene_tags", {})
        target_skill = suggestion.get("suggestion", {}).get("target_skill", "")
        
        # 1. 查找匹配的 cluster
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
            # 2a. 检查语义一致性
            cluster_file = self.clusters_dir / f"{best_match}.yaml"
            with open(cluster_file) as f:
                cluster = yaml.safe_load(f)
            
            if self._check_semantic_consistency(suggestion, cluster):
                # 一致 → 加入 cluster
                cluster["suggestions"].append(suggestion)
                cluster["updated_at"] = datetime.now(timezone.utc).isoformat()
                # 重新计算统计
                cluster["avg_causal_strength"] = self._avg_causal_strength(cluster["suggestions"])
                cluster["consistency_rate"] = self._consistency_rate(cluster["suggestions"])
                
                # 检查 cluster 大小限制
                if len(cluster["suggestions"]) > self.cfg.buffer.max_cluster_size:
                    # 按 LRU 淘汰最旧的
                    cluster["suggestions"].sort(key=lambda s: s.get("created_at", ""))
                    cluster["suggestions"] = cluster["suggestions"][-self.cfg.buffer.max_cluster_size:]
                
                with open(cluster_file, "w") as f:
                    yaml.dump(cluster, f, allow_unicode=True, default_flow_style=False)
                
                return {"action": "added_to_cluster", "cluster_id": best_match}
            else:
                # 不一致 → 创建新 cluster
                return self._create_cluster(suggestion, scene_tags, target_skill)
        else:
            # 2b. 无匹配 → 创建新 cluster
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
        """计算两组场景标签的相似度（0-1）。
        
        匹配维度：role, critical_phase, wolf_aggression, result
        完全匹配 = 1.0，完全不匹配 = 0.0
        """
        key_fields = ["role", "critical_phase", "wolf_aggression", "result",
                       "sheriff_contested", "first_night_target"]
        match_count = 0
        total = len(key_fields)
        
        for field in key_fields:
            val_a = tags_a.get(field)
            val_b = tags_b.get(field)
            if val_a is not None and val_b is not None:
                if str(val_a) == str(val_b):
                    match_count += 1
                elif field == "role_survived_rounds":
                    # 数值型，允许 ±1 误差
                    if abs(int(val_a) - int(val_b)) <= 1:
                        match_count += 0.5
        
        return match_count / total if total > 0 else 0

    def _check_semantic_consistency(self, new_suggestion: Dict, cluster: Dict) -> bool:
        """用 LLM 判断新建议与 cluster 内已有建议是否语义一致。
        
        只比较方向（direction）和目标（target_skill），不需要复杂推理。
        """
        if not cluster.get("suggestions"):
            return True
        
        new_text = new_suggestion.get("suggestion", {}).get("text", "")
        new_direction = new_suggestion.get("suggestion", {}).get("direction", "")
        
        # 快速路径：方向矛盾直接不一致
        existing_directions = [
            s.get("suggestion", {}).get("direction", "")
            for s in cluster["suggestions"]
        ]
        if new_direction == "discard" and "modify" in existing_directions:
            return False
        if new_direction == "modify" and all(d == "discard" for d in existing_directions):
            return False
        
        # 用 LLM 判断语义一致性
        existing_texts = [
            s.get("suggestion", {}).get("text", "")
            for s in cluster["suggestions"][-3:]  # 只取最近 3 条比较
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
            # LLM 调用失败时保守处理：认为一致（宁可多聚类也不遗漏）
            return True

    def _avg_causal_strength(self, suggestions: List[Dict]) -> float:
        strengths = [s.get("suggestion", {}).get("causal_strength", 0) for s in suggestions]
        return sum(strengths) / len(strengths) if strengths else 0

    def _consistency_rate(self, suggestions: List[Dict]) -> float:
        """计算 cluster 内建议方向的一致率。"""
        if not suggestions:
            return 0
        directions = [s.get("suggestion", {}).get("direction", "") for s in suggestions]
        from collections import Counter
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
```

---

## 七、确认判定 (`confirmation.py`)

### 文件: `app/evolution/confirmation.py`

```python
"""evolution/confirmation.py — 双重确认判定

判定一个 cluster 是否满足条件执行策略更新：
  维度一：频率阈值（cluster 内建议数 ≥ N_min 且一致率 ≥ R_min）
  维度二：因果强度阈值（平均 causal_strength ≥ C_min）
  
特殊通道：高因果强度快速确认（causal_strength ≥ 0.8 → 只需 2 次）
"""
import yaml
from pathlib import Path
from typing import Dict, Optional, Tuple
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
        """遍历所有 cluster，执行确认判定。
        
        返回被确认的 cluster 列表（含判定详情）。
        """
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
                # 执行策略更新
                self._execute_confirmation(cluster, cluster_id)
        
        return confirmed

    def judge(self, cluster: Dict) -> Dict:
        """对单个 cluster 执行双重判定。
        
        返回：
        {
            "confirmed": bool,
            "reason": str,
            "count": int,
            "consistency_rate": float,
            "avg_causal_strength": float,
            "fast_track": bool,
        }
        """
        suggestions = cluster.get("suggestions", [])
        count = len(suggestions)
        consistency = cluster.get("consistency_rate", 0)
        avg_causal = cluster.get("avg_causal_strength", 0)
        
        cfm = self.cfg.confirmation
        
        # 快速通道：高因果强度
        if avg_causal >= cfm.fast_track_min_causal_strength:
            if count >= cfm.fast_track_min_count:
                return {
                    "confirmed": True,
                    "reason": f"Fast track: causal={avg_causal:.2f} ≥ {cfm.fast_track_min_causal_strength}, count={count} ≥ {cfm.fast_track_min_count}",
                    "count": count,
                    "consistency_rate": consistency,
                    "avg_causal_strength": avg_causal,
                    "fast_track": True,
                }
            else:
                return {
                    "confirmed": False,
                    "reason": f"Fast track eligible but count={count} < {cfm.fast_track_min_count}",
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
                "reason": f"Normal: count={count}≥{cfm.normal_min_count}, consistency={consistency:.2f}≥{cfm.normal_min_consistency_rate}, causal={avg_causal:.2f}≥{cfm.normal_min_avg_causal_strength}",
                "count": count,
                "consistency_rate": consistency,
                "avg_causal_strength": avg_causal,
                "fast_track": False,
            }
        
        # 未通过
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
        
        # 合成新策略内容
        new_content = self._synthesize_strategy(suggestions, target_skill)
        
        # 创建新版本
        self.version_manager.create_new_version(
            skill_name=target_skill,
            content=new_content,
            source="debounced_update",
            trigger_cluster=cluster_id,
        )
        
        # 将 cluster 移入 confirmed/
        self.buffer_pool.move_to_confirmed(cluster_id)

    def _synthesize_strategy(self, suggestions: list, target_skill: str) -> str:
        """将多条建议合成为一份完整的策略文档。
        
        使用 LLM 合成。输入是 cluster 内所有建议的文本 + 因果链，
        输出一份标准格式的策略 Markdown。
        """
        from agents.llm_caller import llm
        
        suggestions_text = "\n".join(
            f"- 建议 {i+1}: {s.get('suggestion', {}).get('text', '')}\n"
            f"  因果强度: {s.get('suggestion', {}).get('causal_strength', 0)}\n"
            f"  因果链: {self._format_causal_chain(s.get('causal_chain', []))}"
            for i, s in enumerate(suggestions)
        )
        
        # 加载当前版本内容作为基础
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
        except Exception as e:
            # LLM 失败时降级：直接用建议文本拼接
            return self._fallback_synthesize(suggestions, target_skill)

    def _format_causal_chain(self, chain: list) -> str:
        parts = []
        for step in chain:
            parts.append(f"{step.get('action', '')} → {step.get('intermediate', '')} → {step.get('outcome', '')}")
        return " | ".join(parts)

    def _fallback_synthesize(self, suggestions: list, target_skill: str) -> str:
        """LLM 不可用时的降级合成。"""
        lines = [
            f"---",
            f"name: {target_skill}",
            f"description: 基于 {len(suggestions)} 局对局反思的策略",
            f"source: debounced_update",
            f"---",
            f"",
            f"## When to Use",
            f"当相关场景出现时参考此策略。",
            f"",
            f"## Procedure",
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
```

---

## 八、版本管理器 (`version_manager.py`)

### 文件: `app/evolution/version_manager.py`

```python
"""evolution/version_manager.py — 策略版本管理

封装 SkillLoader 中版本操作的高层 API，供 confirmation.py 和 API 层调用。
"""
from evolution.config import EvolutionConfig
from evolution.skill_loader import SkillLoader


class VersionManager:
    """策略版本管理门面。委托 SkillLoader 完成实际操作。"""
    
    def __init__(self, cfg: EvolutionConfig):
        self.cfg = cfg
        self.loader = SkillLoader(cfg)

    def create_new_version(self, skill_name: str, content: str,
                           source: str = "debounced_update",
                           trigger_cluster: str = "") -> str:
        return self.loader.create_new_version(skill_name, content, source, trigger_cluster)

    def rollback(self, skill_name: str, target_version: str) -> bool:
        return self.loader.rollback(skill_name, target_version)

    def load_skill_full(self, skill_name: str, version: str = None) -> str:
        return self.loader.load_skill_full(skill_name, version) or ""

    def get_version_for_game(self, skill_name: str) -> str:
        return self.loader.get_version_for_game(skill_name)

    def record_usage(self, skill_name: str, version: str, won: bool):
        self.loader.record_version_usage(skill_name, version, won)

    def get_status(self, skill_name: str) -> dict:
        meta = self.loader._load_versions_meta(skill_name)
        return meta or {}

    def format_skills_for_prompt(self, my_role: str, phase: str) -> str:
        """组装注入 prompt 的策略文本（Layer 1 索引 + Layer 2 全文）。"""
        index_text = self.loader.format_index_for_prompt(my_role)
        full_text = self.loader.load_skills_for_context(my_role, phase)
        
        parts = []
        if index_text:
            parts.append(index_text)
        if full_text:
            parts.append("## Active Strategy Details")
            parts.append(full_text)
        
        return "\n\n".join(parts)
```

---

## 九、即时标记 (`in_game_flagger.py`)

### 文件: `app/evolution/in_game_flagger.py`

```python
"""evolution/in_game_flagger.py — 对局中即时标记

通过 prompt 注入让 Agent 在执行策略时发现矛盾并标记。
标记不修改策略，只附加到当前对局 trace 中，
等对局结束后由反思引擎处理。
"""
from typing import List, Dict, Any


# ── 注入到 act 节点 system prompt 的指令 ────────────────────

IN_GAME_FLAG_PROMPT = """
当你执行策略时发现策略与实际局势不符（例如策略前提不成立、分支缺失、推荐行动明显不合理）：
1. 不要惊慌，先按当前最佳判断继续行动
2. 在你的思考过程中标记："[FLAG] 策略 X 与当前局势矛盾：原因 Y"
3. 这个标记会被自动记录，在对局结束后用于策略优化

重要：不要在对局中试图大幅修改你的策略思路，保持行为一致性。

同时，在执行决策前评估当前局势与已有策略的匹配度：
- 高匹配（策略明确覆盖当前场景）→ 按策略执行
- 中匹配（策略部分覆盖，局势有差异）→ 策略参考 + 现场调整
- 低匹配 / 策略无覆盖 → 独立判断，不要被动跟风或沉默
"""


class InGameFlagger:
    """从 Agent 的思考过程中提取即时标记。
    
    工作方式：
    1. 在 act 节点的 prompt 中注入 IN_GAME_FLAG_PROMPT
    2. Agent 在思考时如果写了 "[FLAG] ..." 标记
    3. 从 last_thought 中用正则提取标记
    4. 附加到当前对局的 trace 中
    """

    FLAG_PATTERN = r'\[FLAG\]\s*(.+?)(?=\n|$)'

    def extract_flags(self, thought_text: str) -> List[Dict[str, Any]]:
        """从 Agent 的思考文本中提取 [FLAG] 标记。"""
        import re
        flags = []
        for match in re.finditer(self.FLAG_PATTERN, thought_text):
            flags.append({
                "type": "strategy_mismatch",
                "description": match.group(1).strip(),
                "detected_in": "thought",
            })
        return flags

    def get_prompt_injection(self) -> str:
        """返回要注入到 act prompt 中的指令文本。"""
        return IN_GAME_FLAG_PROMPT
```

---

## 十、四层记忆系统 (`app/memory/`)

### 10.1 目录结构

```
app/memory/
├── __init__.py
├── working_memory.py      # Layer 1: 单局工作记忆
├── opponent_model.py       # Layer 2: 对手建模
├── self_model.py           # Layer 3: 自我画像
└── game_archive.py         # Layer 4: 对局历史
```

### 10.2 Layer 1: 单局工作记忆 (`working_memory.py`)

```python
"""memory/working_memory.py — 单局工作记忆

生命周期：单局（从发牌到游戏结束）
加载方式：始终在 prompt 中
Token 预算：~2,000 tokens（滚动压缩）

设计要点：
  - 结构化模板，不是自由文本
  - 分阶段滚动压缩（夜晚/白天发言/投票）
  - 自己的发言完整保留（一致性检查用）
  - 矛盾检测自动运行
"""
import json
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field


@dataclass
class WorkingMemory:
    """单局工作记忆，嵌入 AgentState 中传递。"""
    game_id: str = ""
    my_role: str = ""
    my_seat: str = ""
    day: int = 1
    
    # 已知信息（仅我的视角）
    known_info: List[str] = field(default_factory=list)
    
    # 发言摘要（按天压缩）
    speeches: Dict[str, Dict[str, str]] = field(default_factory=dict)
    # 格式: {"D1": {"1": "怀疑3号", "2": "为3号辩护"}, "D2": {...}}
    
    # 行为记录
    actions: List[str] = field(default_factory=list)
    # 格式: ["D1_vote: 1号出局(6票)", "N1_check: 2号=狼人"]
    
    # 我的发言（完整保留）
    my_speeches: Dict[str, str] = field(default_factory=dict)
    # 格式: {"D1": "我是平民，觉得3号可疑"}
    
    # 矛盾检测
    contradictions: List[str] = field(default_factory=list)
    
    # 即时标记
    flags: List[Dict] = field(default_factory=list)
    
    # 怀疑度排名
    suspicion: Dict[str, List[str]] = field(default_factory=lambda: {"高": [], "中": [], "低": []})

    def update_from_event(self, event: Dict):
        """从 perceive 事件更新工作记忆。"""
        status = event.get("status", "")
        content = event.get("content", "")
        round_num = event.get("round", 1)
        traces = event.get("traces", [])
        
        day_key = f"D{round_num}"
        
        # 夜间事件 → 压缩格式
        if "night" in status or status in ("guard_action", "wolf_kill", "seer_check", "witch_action"):
            for t in traces:
                self.actions.append(f"R{round_num}_{status}: {t.get('from','')}→{t.get('to','')}({t.get('action','')})")
            if content:
                self.known_info.append(f"R{round_num}: {content}")
        
        # 发言事件 → 压缩摘要（实际摘要由 LLM 在 reflect 中完成）
        elif status == "discussion":
            self.speeches.setdefault(day_key, {})
            self.speeches[day_key]["summary"] = content  # 简化版，后续可替换为 LLM 摘要
        
        # 投票事件
        elif status == "vote" or status == "vote_result":
            self.actions.append(f"{day_key}_vote: {content}")
        
        # 死亡通知
        elif status == "death_notice":
            self.actions.append(f"{day_key}_death: {content}")
        
        # 警长
        elif status == "sheriff":
            self.actions.append(f"sheriff: {content}")

    def add_my_speech(self, day: int, text: str):
        """记录我的完整发言。"""
        self.my_speeches[f"D{day}"] = text

    def add_flag(self, flag: Dict):
        """添加即时标记。"""
        self.flags.append(flag)

    def format_for_prompt(self) -> str:
        """格式化为 prompt 片段（~2000 tokens 预算）。"""
        lines = [
            "## Working Memory (This Game)",
            f"Role: {self.my_role} | Seat: {self.my_seat} | Day: {self.day}",
        ]
        
        if self.known_info:
            lines.append("\n### Known Info")
            lines.extend(f"- {info}" for info in self.known_info[-10:])  # 最近 10 条
        
        if self.speeches:
            lines.append("\n### Speech Summaries")
            for day_key, day_speeches in sorted(self.speeches.items()):
                for speaker, content in day_speeches.items():
                    lines.append(f"- {day_key} {speaker}: {content}")
        
        if self.actions:
            lines.append("\n### Key Actions")
            lines.extend(f"- {a}" for a in self.actions[-15:])  # 最近 15 条
        
        if self.my_speeches:
            lines.append("\n### My Speeches (Full)")
            for day_key, text in sorted(self.my_speeches.items()):
                lines.append(f"- {day_key}: \"{text}\"")
        
        if self.contradictions:
            lines.append("\n### ⚠️ Contradictions Detected")
            lines.extend(f"- {c}" for c in self.contradictions)
        
        if self.suspicion.get("高"):
            lines.append(f"\n### Suspicion: HIGH={self.suspicion['高']}, MED={self.suspicion.get('中', [])}, LOW={self.suspicion.get('低', [])}")
        
        return "\n".join(lines)

    def compress_old_entries(self, keep_recent: int = 5):
        """压缩旧条目，控制 token 预算。
        
        保留最近 keep_recent 条完整记录，更早的合并为一行摘要。
        """
        if len(self.actions) > keep_recent * 2:
            old = self.actions[:-keep_recent]
            self.actions = [f"[Earlier: {len(old)} events compressed]"] + self.actions[-keep_recent:]
```

### 10.3 Layer 2: 对手建模 (`opponent_model.py`)

```python
"""memory/opponent_model.py — 跨局对手行为画像

存储：每个对手一个 YAML 文件 (~/.werewolf-agent/memory/opponents/{player_id}.yaml)
加载：开局时按本桌玩家 ID 检索
更新：每局结束后增量更新（LLM 分析本局行为 → 合并到画像）
"""
import yaml
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime, timezone

from evolution.config import AGENT_HOME


OPPONENTS_DIR = AGENT_HOME / "memory" / "opponents"


def load_opponent(player_id: str) -> Optional[Dict]:
    """加载指定对手的画像。"""
    f = OPPONENTS_DIR / f"{player_id}.yaml"
    if f.exists():
        with open(f) as fh:
            return yaml.safe_load(fh)
    return None


def load_opponents_for_table(player_ids: List[str]) -> Dict[str, Dict]:
    """加载本桌所有对手的画像。"""
    result = {}
    for pid in player_ids:
        data = load_opponent(pid)
        if data:
            result[pid] = data
    return result


def format_opponents_for_prompt(opponents: Dict[str, Dict]) -> str:
    """将对手画像格式化为 prompt 片段（每人 ~300 tokens）。"""
    if not opponents:
        return ""
    
    lines = ["## Opponent Profiles"]
    for pid, data in opponents.items():
        lines.append(f"\n### Player {pid}")
        lines.append(f"- Games: {data.get('total_games', 0)} | Win Rate: {data.get('win_rate', 0):.0%}")
        
        wolf = data.get("wolf_behavior", {})
        if wolf:
            lines.append(f"- Wolf: bluff_rate={wolf.get('bluff_rate', 'N/A')}, kill_pref={wolf.get('kill_preference', 'N/A')}")
        
        good = data.get("good_behavior", {})
        if good:
            lines.append(f"- Good: vote_independence={good.get('vote_independence', 'N/A')}")
        
        weaknesses = data.get("weaknesses", [])
        if weaknesses:
            lines.append(f"- Weaknesses: {'; '.join(weaknesses[:3])}")
    
    return "\n".join(lines)


UPDATE_PROMPT = """分析以下对手在本局中的行为，更新其画像。

对手 ID: {player_id}
本局角色: {role}
本局行为摘要:
{behavior_summary}

当前画像:
{current_profile}

输出更新后的完整 YAML（保持相同结构，只更新数据）：
```yaml
player_id: "{player_id}"
total_games: <累计对局数>
last_seen: <今天日期 ISO 格式>
win_rate: <总胜率 0-1>
wolf_behavior:
  bluff_rate: <悍跳频率 0-1>
  kill_preference: <首夜刀人偏好描述>
  reaction_when_suspected: <被怀疑时的反应>
good_behavior:
  seer_jump_timing: <预言家跳身份时机>
  vote_independence: <投票独立性 0-1>
weaknesses:
  - <可利用弱点1>
  - <可利用弱点2>
recent_games:
  - <最近3局的简要记录>
```"""


def update_opponent_from_game(player_id: str, role: str,
                                behavior_summary: str,
                                llm_caller) -> bool:
    """对局结束后增量更新对手画像。"""
    current = load_opponent(player_id)
    current_text = yaml.dump(current, allow_unicode=True) if current else "（无历史画像）"
    
    prompt = UPDATE_PROMPT.format(
        player_id=player_id,
        role=role,
        behavior_summary=behavior_summary,
        current_profile=current_text,
    )
    
    try:
        resp = llm_caller.client.chat.completions.create(
            model=llm_caller.model,
            messages=[
                {"role": "system", "content": "你是狼人杀玩家行为分析专家。输出 YAML。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        text = resp.choices[0].message.content or ""
        
        # 提取 YAML
        import re
        match = re.search(r'```(?:yaml)?\s*\n(.*?)```', text, re.DOTALL)
        yaml_text = match.group(1) if match else text
        
        updated = yaml.safe_load(yaml_text)
        if updated and isinstance(updated, dict):
            OPPONENTS_DIR.mkdir(parents=True, exist_ok=True)
            with open(OPPONENTS_DIR / f"{player_id}.yaml", "w") as f:
                yaml.dump(updated, f, allow_unicode=True, default_flow_style=False)
            return True
    except Exception:
        pass
    return False
```

### 10.4 Layer 3: 自我画像 (`self_model.py`)

```python
"""memory/self_model.py — 自我画像

存储：~/.werewolf-agent/memory/self_model/profile.yaml
加载：始终加载到 system prompt（精简版 ~500 tokens）
更新：每局结束后增量更新
"""
import yaml
from pathlib import Path
from typing import Dict, Optional
from datetime import datetime, timezone

from evolution.config import AGENT_HOME

SELF_MODEL_PATH = AGENT_HOME / "memory" / "self_model" / "profile.yaml"


def load_self_model() -> Optional[Dict]:
    if SELF_MODEL_PATH.exists():
        with open(SELF_MODEL_PATH) as f:
            return yaml.safe_load(f)
    return None


def format_self_model_for_prompt() -> str:
    """格式化为 prompt 片段。"""
    model = load_self_model()
    if not model:
        return ""
    
    lines = ["## Self Profile (Your Historical Performance)"]
    
    # 各角色胜率
    role_stats = model.get("role_win_rates", {})
    if role_stats:
        rates = ", ".join(f"{r}: {v:.0%}" for r, v in role_stats.items())
        lines.append(f"Win Rates: {rates}")
    
    # 常见失误
    mistakes = model.get("common_mistakes", [])
    if mistakes:
        lines.append(f"Common Mistakes: {'; '.join(mistakes[:3])}")
    
    # 强项
    strengths = model.get("strengths", [])
    if strengths:
        lines.append(f"Strengths: {'; '.join(strengths[:3])}")
    
    return "\n".join(lines)


UPDATE_PROMPT = """根据本局表现更新 Agent 的自我画像。

我的角色: {my_role}
结果: {result}
关键决策回顾:
{key_decisions}

当前画像:
{current_profile}

输出更新后的 YAML:
```yaml
total_games: <累计>
role_win_rates:
  seer: <0-1>
  wolf: <0-1>
  witch: <0-1>
  # ... 各角色
common_mistakes:
  - "<失误模式1>"
  - "<失误模式2>"
strengths:
  - "<强项1>"
improvement_areas:
  - "<改进方向1>"
recent_form: <最近5局的胜负列表>
```"""


def update_self_model(my_role: str, result: str,
                       key_decisions: str, llm_caller) -> bool:
    """对局结束后增量更新自我画像。"""
    current = load_self_model()
    current_text = yaml.dump(current, allow_unicode=True) if current else "（无历史画像）"
    
    prompt = UPDATE_PROMPT.format(
        my_role=my_role, result=result,
        key_decisions=key_decisions, current_profile=current_text,
    )
    
    try:
        resp = llm_caller.client.chat.completions.create(
            model=llm_caller.model,
            messages=[
                {"role": "system", "content": "你是 AI 玩家自我评估专家。输出 YAML。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        text = resp.choices[0].message.content or ""
        import re
        match = re.search(r'```(?:yaml)?\s*\n(.*?)```', text, re.DOTALL)
        yaml_text = match.group(1) if match else text
        
        updated = yaml.safe_load(yaml_text)
        if updated:
            SELF_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(SELF_MODEL_PATH, "w") as f:
                yaml.dump(updated, f, allow_unicode=True, default_flow_style=False)
            return True
    except Exception:
        pass
    return False
```

### 10.5 Layer 4: 对局历史 (`game_archive.py`)

```python
"""memory/game_archive.py — 对局历史归档 (SQLite)

存储：~/.werewolf-agent/memory/game_archive/games.db
加载：按需检索（按角色/场景/对手过滤）
更新：每局结束后写入

表结构：
  games:
    - game_id TEXT PK
    - my_role TEXT
    - result TEXT (won/lost)
    - day_count INTEGER
    - scene_tags JSON
    - reflection_report TEXT (YAML)
    - full_trace TEXT
    - strategies_used JSON
    - created_at TEXT
  
  strategy_gaps:
    - id INTEGER PK
    - game_id TEXT FK
    - scene_description TEXT
    - gap_count INTEGER (该场景累计 gap 次数)
"""
import sqlite3
import json
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime, timezone

from evolution.config import AGENT_HOME

DB_PATH = AGENT_HOME / "memory" / "game_archive" / "games.db"


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    _ensure_tables(conn)
    return conn


def _ensure_tables(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS games (
            game_id TEXT PRIMARY KEY,
            my_role TEXT,
            result TEXT,
            day_count INTEGER,
            scene_tags TEXT,
            reflection_report TEXT,
            full_trace TEXT,
            strategies_used TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS strategy_gaps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id TEXT,
            scene_description TEXT,
            gap_count INTEGER DEFAULT 1,
            FOREIGN KEY (game_id) REFERENCES games(game_id)
        );
        CREATE INDEX IF NOT EXISTS idx_games_role ON games(my_role);
        CREATE INDEX IF NOT EXISTS idx_games_result ON games(result);
    """)
    conn.commit()


def save_game(game_id: str, my_role: str, result: str, day_count: int,
              scene_tags: Dict, reflection_report: str, full_trace: str,
              strategies_used: List[str]):
    """保存一局对局记录。"""
    conn = get_connection()
    conn.execute("""
        INSERT OR REPLACE INTO games
        (game_id, my_role, result, day_count, scene_tags, reflection_report,
         full_trace, strategies_used, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        game_id, my_role, result, day_count,
        json.dumps(scene_tags, ensure_ascii=False),
        reflection_report, full_trace,
        json.dumps(strategies_used),
        datetime.now(timezone.utc).isoformat(),
    ))
    conn.commit()
    conn.close()


def query_games(my_role: Optional[str] = None, result: Optional[str] = None,
                limit: int = 10) -> List[Dict]:
    """按条件检索历史对局。"""
    conn = get_connection()
    query = "SELECT * FROM games WHERE 1=1"
    params = []
    if my_role:
        query += " AND my_role = ?"
        params.append(my_role)
    if result:
        query += " AND result = ?"
        params.append(result)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def record_strategy_gap(game_id: str, scene_description: str):
    """记录一次 strategy_gap（策略覆盖空白）。"""
    conn = get_connection()
    # 检查同场景是否已有记录
    row = conn.execute(
        "SELECT id, gap_count FROM strategy_gaps WHERE scene_description = ?",
        (scene_description,)
    ).fetchone()
    
    if row:
        conn.execute(
            "UPDATE strategy_gaps SET gap_count = gap_count + 1 WHERE id = ?",
            (row["id"],)
        )
    else:
        conn.execute(
            "INSERT INTO strategy_gaps (game_id, scene_description) VALUES (?, ?)",
            (game_id, scene_description)
        )
    conn.commit()
    conn.close()


def get_frequent_gaps(min_count: int = 5) -> List[Dict]:
    """获取频繁出现的 strategy_gap（≥ min_count 次），提示人工创建策略。"""
    conn = get_connection()
    rows = conn.execute(
        "SELECT scene_description, gap_count FROM strategy_gaps WHERE gap_count >= ? ORDER BY gap_count DESC",
        (min_count,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
```

---

## 十一、Curator (`curator.py`)

### 文件: `app/evolution/curator.py`

```python
"""evolution/curator.py — 自主策展人

两阶段策略库维护：
  阶段一：确定性状态转移（active → stale → archived）
  阶段二：LLM 审查（keep / patch / consolidate / archive）

触发条件（空闲触发）：
  - 距上次运行 ≥ interval_hours（默认 168h = 7 天）
  - Agent 当前无对局进行中

备份：每次运行前自动快照
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
            # 首次运行：只记录时间，不实际运行
            self._save_state({"last_run_at": datetime.now(timezone.utc).isoformat()})
            return False
        
        hours_since = (datetime.now(timezone.utc) - datetime.fromisoformat(last_run)).total_seconds() / 3600
        return hours_since >= self.cfg.curator.interval_hours

    def run(self) -> Dict:
        """执行 Curator 审查。返回操作摘要。"""
        # 1. 自动快照
        self._snapshot()
        
        summary = {"phase1": {}, "phase2": {}}
        
        # 2. 阶段一：确定性状态转移
        summary["phase1"] = self._phase1_state_transitions()
        
        # 3. 阶段二：LLM 审查
        summary["phase2"] = self._phase2_llm_review()
        
        # 4. 更新运行时间
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
        review_llm.model = self.cfg.clustering_model  # 用轻量模型
        
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
            # patch 和 consolidate 暂不自动执行，只记录
            # （完整版可在 Phase 3 实现）
        
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
        return "keep"  # 保守默认

    def _snapshot(self):
        """创建技能库快照。"""
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        snapshot_dir = self.backup_dir / ts
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        
        tar_path = snapshot_dir / "skills.tar.gz"
        with tarfile.open(str(tar_path), "w:gz") as tar:
            tar.add(str(self.skills_root), arcname="skills")
        
        # 保留最近 5 个快照
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
```

---

## 十二、API 集成 — 扩展 `main_ws.py`

### 12.1 新增消息类型

在 WebSocket 协议中新增以下消息类型：

```python
# ── 新增消息类型 ───────────────────────────────────────────

# 客户端 → 服务端:
#   game_over   {"type": "game_over", "req_id": "...", "session_id": "...",
#                "result": "won"/"lost", "winner_role": "...",
#                "all_roles": {"1": "wolf", "2": "seer", ...}}

# 服务端 → 客户端:
#   reflect_ok  {"type": "reflect_ok", "req_id": "...",
#                "suggestion_id": "...", "buffer_status": "buffered"/"skipped"/...}
#   buffer_status {"type": "buffer_status", ...}
```

### 12.2 在 `main_ws.py` 中新增处理逻辑

在 `handle_connection()` 函数的消息分发中添加：

```python
# 在 handle_connection() 中的 elif 链中追加：

elif msg_type == "game_over":
    resp = await _process_game_over(
        _route_thread_id(msg, agent_id or ""),
        req_id,
        msg.get("result", "lost"),
        msg.get("winner_role", ""),
        msg.get("all_roles", {}),
    )

elif msg_type == "buffer_status":
    resp = await _process_buffer_status(
        _route_thread_id(msg, agent_id or ""),
        req_id,
    )

elif msg_type == "rollback":
    resp = await _process_rollback(
        req_id,
        msg.get("skill_name", ""),
        msg.get("target_version", ""),
    )
```

### 12.3 实现函数

```python
# ── game_over 处理 ────────────────────────────────────────

async def _process_game_over(session_id: str, req_id: str,
                              result: str, winner_role: str,
                              all_roles: dict) -> dict:
    """对局结束：触发反思 + 记忆更新 + 缓冲池操作。"""
    state = store.get(session_id)
    if state is None:
        return {"type": "error", "req_id": req_id,
                "detail": "Session not found."}
    
    # 异步执行，不阻塞 WebSocket
    asyncio.create_task(_run_post_game_pipeline(
        state, result, winner_role, all_roles, session_id, req_id
    ))
    
    return {"type": "game_over_ack", "req_id": req_id}


async def _run_post_game_pipeline(state: dict, result: str,
                                   winner_role: str, all_roles: dict,
                                   session_id: str, req_id: str):
    """对局结束后完整管道：反思 → 缓冲 → 记忆更新。"""
    from evolution.config import load_config, ensure_directories
    from evolution.reflection_engine import ReflectionEngine, format_game_trace
    from evolution.buffer_pool import BufferPool
    from evolution.clustering import SuggestionClusterer
    from evolution.confirmation import ConfirmationJudge
    from evolution.version_manager import VersionManager
    from evolution.in_game_flagger import InGameFlagger
    from memory.game_archive import save_game, record_strategy_gap
    from memory.self_model import update_self_model
    from memory.opponent_model import update_opponent_from_game
    
    cfg = load_config()
    ensure_directories(cfg)
    
    # 1. 格式化 trace
    game_trace = format_game_trace(state.get("events", []), state.get("players", {}))
    
    # 2. 提取即时标记
    flagger = InGameFlagger()
    flags = flagger.extract_flags(state.get("last_thought", ""))
    
    # 3. 加载当前使用的策略
    vm = VersionManager(cfg)
    current_strategies = vm.format_skills_for_prompt(
        state["my_role"], state.get("phase", "")
    )
    
    # 4. 执行反思
    engine = ReflectionEngine(cfg)
    reflection = engine.reflect(
        game_id=state.get("room_id", "unknown"),
        my_role=state["my_role"],
        my_seat=state["me_id"],
        result=result,
        game_trace=game_trace,
        in_game_flags=flags,
        current_strategies=current_strategies,
    )
    
    if reflection:
        # 5. 写入缓冲池
        pool = BufferPool(cfg)
        status = pool.ingest(reflection)
        
        # 6. 触发聚类
        clusterer = SuggestionClusterer(cfg)
        clusterer.process_pending()
        
        # 7. 检查确认
        judge = ConfirmationJudge(cfg, pool, vm)
        confirmed = judge.check_all_clusters()
        
        # 8. 记录 strategy_gap
        if reflection.suggestion.match_level in ("low", "strategy_gap"):
            record_strategy_gap(
                reflection.game_id,
                f"{reflection.scene_tags.role}_{reflection.scene_tags.critical_phase}"
            )
        
        # 9. 归档对局
        save_game(
            game_id=reflection.game_id,
            my_role=reflection.my_role,
            result=result,
            day_count=state.get("day", 1),
            scene_tags={
                "role": reflection.scene_tags.role,
                "result": reflection.scene_tags.result,
                "wolf_aggression": reflection.scene_tags.wolf_aggression,
            },
            reflection_report=yaml_dump_reflection(reflection),
            full_trace=game_trace,
            strategies_used=[],  # TODO: 从 state 中提取实际使用的策略
        )
    
    # 10. 更新自我画像（用轻量 LLM 调用）
    from agents.llm_caller import llm
    update_self_model(
        my_role=state["my_role"],
        result=result,
        key_decisions=state.get("last_thought", ""),
        llm_caller=llm,
    )
    
    # 11. 清理过期建议（定期执行）
    pool = BufferPool(cfg)
    pool.expire_old_suggestions()
    
    logger.info(f"Post-game pipeline complete for session {session_id}: result={result}")


def yaml_dump_reflection(reflection) -> str:
    """将 ReflectionResult 序列化为 YAML 字符串。"""
    import yaml
    from dataclasses import asdict
    return yaml.dump(asdict(reflection), allow_unicode=True, default_flow_style=False)


# ── buffer_status 处理 ────────────────────────────────────

async def _process_buffer_status(session_id: str, req_id: str) -> dict:
    """返回缓冲池状态。"""
    from evolution.config import load_config
    from evolution.buffer_pool import BufferPool
    
    cfg = load_config()
    pool = BufferPool(cfg)
    status = pool.get_status()
    
    return {"type": "buffer_status", "req_id": req_id, **status}


# ── rollback 处理 ────────────────────────────────────────

async def _process_rollback(req_id: str, skill_name: str,
                             target_version: str) -> dict:
    """回退策略版本。"""
    from evolution.config import load_config
    from evolution.version_manager import VersionManager
    
    cfg = load_config()
    vm = VersionManager(cfg)
    success = vm.rollback(skill_name, target_version)
    
    return {
        "type": "rollback_result",
        "req_id": req_id,
        "success": success,
        "skill_name": skill_name,
        "target_version": target_version,
    }
```

---

## 十三、Prompt 注入点 — 修改 `prompt_builder.py`

### 13.1 修改 `build_decision_prompt`

在现有 `build_decision_prompt()` 方法中，将硬编码的 `CRITICAL_THINKING_FRAMEWORK` 替换为动态加载的策略 + 进化注入：

```python
# 修改 prompt_builder.py 的 build_decision_prompt 方法

def build_decision_prompt(self, state, task_specific_guidance,
                          final_instruction, last_thought="",
                          extra_data=None,
                          include_thinking_framework=True) -> str:
    prompt = self._get_core_task(extra_data)
    prompt += self.get_game_info(state, extra_data)
    
    # ── 新增：注入记忆层 ──
    if extra_data and extra_data.get("working_memory"):
        prompt += "\n---\n" + extra_data["working_memory"].format_for_prompt()
    
    if extra_data and extra_data.get("opponent_profiles"):
        from memory.opponent_model import format_opponents_for_prompt
        prompt += "\n---\n" + format_opponents_for_prompt(extra_data["opponent_profiles"])
    
    if extra_data and extra_data.get("self_model_text"):
        prompt += "\n---\n" + extra_data["self_model_text"]
    
    # ── 修改：动态策略加载（替代硬编码 thinking framework）──
    if include_thinking_framework:
        # 优先使用进化后的策略文档
        strategy_text = ""
        if extra_data and extra_data.get("evolution_strategies"):
            strategy_text = extra_data["evolution_strategies"]
        
        if strategy_text:
            prompt += f"\n---\n## Active Strategies (Evolved)\n{strategy_text}\n---"
        
        # 保留原有 thinking framework 作为兜底
        prompt += f"""

---
This is a thinking framework for reference. Apply flexibly based on your role and the current situation:
``` Thinking Framework
{prompt_storage.CRITICAL_THINKING_FRAMEWORK}
```
---"""
    
    # ── 新增：注入即时标记行为指令 ──
    from evolution.in_game_flagger import IN_GAME_FLAG_PROMPT
    prompt += f"\n---\n{IN_GAME_FLAG_PROMPT}\n"
    
    if last_thought:
        prompt += f"\n### Your Previous Reflection\n{last_thought}\n"
    
    prompt += "\n---\n" + task_specific_guidance + "\n"
    prompt += "\n" + final_instruction + "\n"
    return prompt
```

### 13.2 修改各 decider 函数传入 extra_data

在 `agent_graph.py` 中的各 `_decide_*` 函数中，组装 `extra_data` 传入：

```python
# 在每个 _decide_* 函数中，构建 prompt 前组装 extra_data：

def _build_extra_data(state: AgentState, phase: str) -> dict:
    """组装传入 prompt_builder 的额外数据。"""
    from memory.self_model import format_self_model_for_prompt
    from memory.working_memory import WorkingMemory
    
    extra = {}
    
    # 工作记忆（从 state 中读取或创建）
    wm_data = state.get("working_memory")
    if wm_data:
        wm = WorkingMemory(**wm_data)
    else:
        wm = WorkingMemory(
            game_id=state.get("room_id", ""),
            my_role=state["my_role"],
            my_seat=state["me_id"],
            day=state.get("day", 1),
        )
    extra["working_memory"] = wm
    
    # 自我画像
    extra["self_model_text"] = format_self_model_for_prompt()
    
    # 进化策略
    try:
        from evolution.config import load_config
        from evolution.version_manager import VersionManager
        cfg = load_config()
        vm = VersionManager(cfg)
        extra["evolution_strategies"] = vm.format_skills_for_prompt(
            state["my_role"], phase
        )
    except Exception:
        pass  # 进化模块未初始化时不影响正常决策
    
    return extra
```

---

## 十四、AgentState 扩展

### 修改 `state.py`

```python
class AgentState(TypedDict):
    # ... 现有字段保持不变 ...
    
    # ── 新增：记忆与进化 ──
    working_memory: Optional[Dict[str, Any]]    # Layer 1 工作记忆（序列化）
    strategies_used: List[str]                   # 本局使用的策略版本列表
    in_game_flags: List[Dict[str, Any]]          # 对局中积累的即时标记
```

### 修改 `make_initial_state()`

```python
def make_initial_state(agent_id: str) -> AgentState:
    # ... 现有代码 ...
    
    state["working_memory"] = {
        "game_id": "unknown",
        "my_role": role_val,
        "my_seat": player_id,
        "day": 1,
        "known_info": [],
        "speeches": {},
        "actions": [],
        "my_speeches": {},
        "contradictions": [],
        "flags": [],
        "suspicion": {"高": [], "中": [], "低": []},
    }
    state["strategies_used"] = []
    state["in_game_flags"] = []
    
    return state
```

---

## 十五、依赖更新

### 修改 `requirements.txt`

追加：

```
pyyaml>=6.0
```

不需要额外的数据库依赖（使用 Python 内置 sqlite3）。

---

## 十六、端到端数据流图

```
┌─────────────────────── 对局中 ─────────────────────────────────────┐
│                                                                     │
│  [编排侧] perceive → [Agent] run_perceive()                         │
│    → 更新 AgentState (events, players)                              │
│    → 更新 WorkingMemory (working_memory.update_from_event)          │
│                                                                     │
│  [编排侧] act → [Agent] run_act()                                   │
│    → _build_extra_data():                                           │
│        ├─ WorkingMemory.format_for_prompt() → 注入 prompt           │
│        ├─ Self Model → 注入 prompt                                  │
│        ├─ VersionManager.format_skills_for_prompt() → 注入 prompt   │
│        └─ InGameFlagger.IN_GAME_FLAG_PROMPT → 注入 prompt           │
│    → PromptBuilder.build_decision_prompt(extra_data=...)             │
│    → LLM 决策                                                       │
│    → InGameFlagger.extract_flags(last_thought) → 追加到 flags       │
│    → 记录 strategies_used                                           │
│                                                                     │
└─────────────────────────────┬───────────────────────────────────────┘
                              ↓
┌─────────────────────── 对局结束 ────────────────────────────────────┐
│                                                                     │
│  [编排侧] game_over → [Agent] _process_game_over()                  │
│    → _run_post_game_pipeline() (async, 不阻塞):                     │
│                                                                     │
│    ① format_game_trace(events) → 完整 trace 文本                    │
│    ② InGameFlagger.extract_flags() → 即时标记                       │
│    ③ ReflectionEngine.reflect() → ReflectionResult                  │
│       (因果链 + 策略建议 + 场景标签 + causal_strength)              │
│    ④ BufferPool.ingest(result) → 写入 pending/                      │
│       (match_level=low → 跳过, 记录 strategy_gap)                   │
│    ⑤ SuggestionClusterer.process_pending() → 聚类到 clusters/       │
│    ⑥ ConfirmationJudge.check_all_clusters()                         │
│       ├─ 频率 ≥ 阈值 + 因果强度 ≥ 阈值 → 创建新版本                 │
│       ├─ 快速通道 (causal ≥ 0.8, count ≥ 2) → 创建新版本            │
│       └─ 未达标 → 留在 cluster 继续积累                              │
│    ⑦ save_game() → SQLite Layer 4                                   │
│    ⑧ update_self_model() → Layer 3                                  │
│    ⑨ update_opponent_from_game() → Layer 2 (per opponent)           │
│    ⑩ BufferPool.expire_old_suggestions() → 清理过期                 │
│                                                                     │
└─────────────────────────────┬───────────────────────────────────────┘
                              ↓
┌─────────────────────── 后台定期 ────────────────────────────────────┐
│                                                                     │
│  Curator.should_run() → 满足条件时:                                 │
│    ① _snapshot() → skills.tar.gz 备份                               │
│    ② Phase 1: active → stale → archived (确定性)                    │
│    ③ Phase 2: LLM review (keep/patch/consolidate/archive)           │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 十七、实现路线与验收标准

### Phase 1: 最小可行防抖 + 记忆基础（1-2 周）

**实现清单：**

| # | 文件 | 内容 | 验收条件 |
|---|------|------|---------|
| 1 | `evolution/config.py` | 配置加载 | 从 YAML 加载，缺省使用默认值 |
| 2 | `evolution/reflection_engine.py` | 结构化反思 | 对局结束后产出 YAML 格式的因果链+建议 |
| 3 | `evolution/buffer_pool.py` | 缓冲池存储 | 建议写入 pending/，按 match_level 过滤 |
| 4 | `evolution/clustering.py` | 语义聚类 | 同场景建议归入同一 cluster |
| 5 | `evolution/confirmation.py` | 频率判定 | 只实现频率阈值（暂不做因果强度） |
| 6 | `evolution/skill_loader.py` | 策略目录管理 | 创建新版本文件，读取 .versions.json |
| 7 | `evolution/version_manager.py` | 版本管理门面 | 调 create_new_version 能生成 v(n+1).md |
| 8 | `evolution/in_game_flagger.py` | 即时标记提取 | 从 [FLAG] 标记中提取 |
| 9 | `memory/working_memory.py` | 工作记忆 | 结构化记录+格式化输出 |
| 10 | `memory/self_model.py` | 自我画像基础 | 各角色胜率统计 |
| 11 | `memory/game_archive.py` | 对局历史 SQLite | 保存+检索 |
| 12 | `state.py` | AgentState 扩展 | 新增字段 |
| 13 | `prompt_builder.py` | 注入点 | 记忆+策略+flag prompt 注入 |
| 14 | `main_ws.py` | game_over 处理 | 触发完整管道 |

**验收标准：**
- 对局结束后 Agent 自动产出结构化反思建议
- 相似建议能聚类到同一 cluster
- 达到频率阈值后能自动创建新版本策略文件
- 单局工作记忆能记录关键信息
- match_level=low 的建议不进入缓冲池
- `/debug/view` 能看到反思 LLM 调用

### Phase 2: 因果强度 + 版本竞争 + 对手建模（2-3 周）

- [ ] 反思引擎加入 causal_strength 自评
- [ ] 双重判定（频率 + 因果强度）
- [ ] 高因果快速通道
- [ ] 版本竞争（warmup → 统计 → 升降级）
- [ ] `/agent/policy/rollback` 回退
- [ ] 过期清理
- [ ] Layer 2 对手建模
- [ ] strategy_gap 聚合统计（连续 ≥5 局低匹配时告警）

### Phase 3: Curator + 集成优化（2-3 周）

- [ ] Curator 阶段一（stale/archive 自动降级）
- [ ] Curator 阶段二（LLM 审查）
- [ ] Curator 快照与回滚
- [ ] 渐进式加载适配版本化
- [ ] Thompson Sampling 版本分配
- [ ] 所有阈值配置化

### Phase 4: GEPA 离线进化（3-4 周，可选）

- [ ] 策略 → DSPy 包装
- [ ] batch_runner 批量模拟
- [ ] LLM-as-Judge 适应度
- [ ] 帕累托前沿选择
- [ ] GEPA 产出 → 版本竞争

---

## 十八、测试要点

### 单元测试

```
test_reflection_engine.py:
  - test_parse_valid_yaml: LLM 输出合法 YAML 时能正确解析
  - test_parse_invalid_yaml: LLM 输出乱码时返回 None
  - test_causal_strength_multiplier: 即时标记的 ×1.3 加成
  - test_match_level_medium_discount: medium 的 ×0.7 折扣
  - test_match_level_low_skipped: low 不进缓冲池

test_buffer_pool.py:
  - test_ingest_buffered: 正常建议写入 pending/
  - test_ingest_skipped: low match_level 跳过
  - test_expire_old: 超过 30 天的建议移入 expired/

test_clustering.py:
  - test_scene_tag_overlap: 标签匹配度计算
  - test_create_new_cluster: 无匹配时创建新 cluster
  - test_add_to_existing_cluster: 匹配时加入已有 cluster
  - test_semantic_inconsistency: 矛盾建议不入同一 cluster

test_confirmation.py:
  - test_normal_confirm: 频率+因果都达标时确认
  - test_fast_track: 高因果快速通道
  - test_not_enough_count: 次数不足不确认
  - test_low_causal: 因果强度不足不确认

test_version_manager.py:
  - test_create_new_version: 创建 v(n+1).md
  - test_rollback: 回退到指定版本
  - test_version_competition_warmup: warmup 期间的版本分配

test_working_memory.py:
  - test_update_from_event: 事件更新工作记忆
  - test_format_for_prompt: 格式化输出不超 token 预算
  - test_compress_old_entries: 旧条目压缩

test_skill_loader.py:
  - test_load_index: 索引加载
  - test_load_skill_full: 全文加载
  - test_find_skill_dir: 目录查找
```

### 集成测试

```
test_e2e_reflection_pipeline.py:
  模拟一局完整对局 → game_over → 验证:
    1. ReflectionResult 产出
    2. 缓冲池有 pending 建议
    3. 聚类后 cluster 文件存在
    4. 连续 3 局相同建议后确认执行
    5. 新版本文件 v2.md 创建
    6. .versions.json 更新
    7. SQLite 有对局记录
    8. 自我画像更新
```

---

## 十九、关键设计决策记录

| 编号 | 决策 | 选择 | 理由 |
|------|------|------|------|
| D1 | 缓冲池位置 | Agent 层本地 (文件系统) | 策略是 Agent 私有资产，编排层不关心 |
| D2 | 语义聚类方式 | 先用场景标签匹配 + LLM 一致性检查 | embedding 需要额外依赖，标签匹配更确定性 |
| D3 | 版本竞争分配 | Phase 1 用随机 50/50, Phase 3 升级 Thompson Sampling | 先简单验证，再优化 |
| D4 | 反思模型 | 默认用主模型，可配独立模型 | 初期不引入额外模型管理成本 |
| D5 | 策略存储格式 | Markdown + YAML frontmatter | 兼容 agentskills.io 标准，人类可读可编辑 |
| D6 | 记忆存储 | 文件(YAML) + SQLite(对局历史) | 简单场景用文件，需要检索的用 SQLite |
| D7 | 对局中策略修改 | 不修改，只标记 | 防止前半局和后半局行为矛盾 |
| D8 | Curator 触发 | 空闲触发，非 cron | 避免对局中触发影响性能 |

---

## 二十、注意事项

1. **向后兼容**：现有 `/agent/init`、`/agent/perceive`、`/agent/act` 接口不变，新增功能通过新消息类型接入。
2. **优雅降级**：进化模块（`evolution/`）任何异常不应影响正常对局决策。所有进化相关调用都用 try/except 包裹。
3. **Token 预算**：策略注入后 prompt 总长度需要监控。Layer 2 最多加载 3 个策略，每个 ~5000 tokens。如果 prompt 超长，优先裁剪 thinking framework，保留进化策略。
4. **并发安全**：`BufferPool` 和 `SkillLoader` 的文件操作用 `threading.Lock` 保护（参考 `SessionStore` 的实现）。
5. **幂等性**：`game_over` 可能被重发，`_process_game_over` 需要用 `game_id` 做去重。
6. **日志**：所有进化管道操作记入 `prompt_logger`，可在 `/debug/view` 中查看。
