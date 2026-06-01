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
    normal_min_count: int = 3
    normal_min_consistency_rate: float = 0.60
    normal_min_avg_causal_strength: float = 0.50
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
    interval_hours: int = 168  # 7 days
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
    clustering_model: str = "deepseek-chat"
    reflection_model: str = ""
    in_game_flag_causal_multiplier: float = 1.3
    skills_path: str = str(AGENT_HOME / "skills")


def load_config() -> EvolutionConfig:
    """从 YAML 文件加载配置，不存在则返回默认值。"""
    config_path = AGENT_HOME / "config.yaml"
    if config_path.exists():
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}
        dp = raw.get("debounced_policy", {})
        cfg = EvolutionConfig()
        _merge_dataclass(cfg, dp)
        _merge_dataclass(cfg.reflection, dp.get("reflection", {}))
        _merge_dataclass(cfg.buffer, dp.get("buffer", {}))
        _merge_dataclass(cfg.confirmation, dp.get("confirmation", {}).get("normal", {}))
        _merge_dataclass(cfg.versioning, dp.get("versioning", {}))
        _merge_dataclass(cfg.curator, dp.get("curator", {}))
        # top-level overrides
        for k in ("enabled", "clustering_model", "reflection_model",
                  "in_game_flag_causal_multiplier", "skills_path"):
            if k in dp:
                setattr(cfg, k, dp[k])
        return cfg
    return EvolutionConfig()


def _merge_dataclass(obj, overrides: dict):
    """将 dict 中的键值对覆盖到 dataclass 实例上。"""
    for k, v in overrides.items():
        if hasattr(obj, k):
            setattr(obj, k, v)


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
