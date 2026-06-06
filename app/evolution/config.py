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
    max_age_days: int = 30
    max_cluster_size: int = 20
    cleanup_interval_hours: int = 24
    semantic_similarity_threshold: float = 0.75


@dataclass
class ConfirmationConfig:
    normal_min_count: int = 2
    normal_min_consistency_rate: float = 0.50
    normal_min_avg_causal_strength: float = 0.35
    fast_track_min_causal_strength: float = 0.70
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
    interval_hours: int = 12  # 12 hours
    min_idle_hours: int = 2
    max_iterations: int = 8


@dataclass
class GEPAConfig:
    enabled: bool = True
    min_games_for_fitness: int = 5
    min_skills_in_library: int = 2
    num_generations: int = 10
    population_size: int = 8
    mutation_model: str = "deepseek-chat"
    judge_model: str = "deepseek-chat"
    game_service_url: str = "http://host.docker.internal:8081"


@dataclass
class SummaryConfig:
    model: str = "deepseek-v4-pro"


@dataclass
class EvolutionConfig:
    enabled: bool = True
    reflection: ReflectionConfig = field(default_factory=ReflectionConfig)
    buffer: BufferConfig = field(default_factory=BufferConfig)
    confirmation: ConfirmationConfig = field(default_factory=ConfirmationConfig)
    versioning: VersioningConfig = field(default_factory=VersioningConfig)
    curator: CuratorConfig = field(default_factory=CuratorConfig)
    gepa: GEPAConfig = field(default_factory=GEPAConfig)
    summary: SummaryConfig = field(default_factory=SummaryConfig)
    clustering_model: str = "deepseek-chat"
    reflection_model: str = ""
    in_game_flag_causal_multiplier: float = 1.3
    medium_match_causal_discount: float = 0.7


def load_config() -> EvolutionConfig:
    """从 YAML 文件加载配置，不存在则返回默认值。"""
    config_path = Path(os.getenv("WEREWOLF_AGENT_HOME", "~/.werewolf-agent")).expanduser() / "config.yaml"
    if config_path.exists():
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}
        dp = raw.get("debounced_policy", {})
        cfg = EvolutionConfig()

        _merge_dataclass(cfg.reflection, dp.get("reflection", {}))
        _merge_dataclass(cfg.buffer, dp.get("buffer", {}))

        normal_cfg = dp.get("confirmation", {}).get("normal", {})
        fast_cfg = dp.get("confirmation", {}).get("fast_track", {})
        _merge_dataclass(cfg.confirmation, normal_cfg, prefix="normal_")
        _merge_dataclass(cfg.confirmation, fast_cfg, prefix="fast_track_")

        _merge_dataclass(cfg.versioning, dp.get("versioning", {}))
        _merge_dataclass(cfg.curator, dp.get("curator", {}))
        _merge_dataclass(cfg.gepa, dp.get("gepa", {}))
        _merge_dataclass(cfg.summary, dp.get("summary", {}))

        for k in ("enabled", "clustering_model", "reflection_model",
                  "in_game_flag_causal_multiplier", "medium_match_causal_discount"):
            if k in dp:
                setattr(cfg, k, dp[k])
        return cfg
    return EvolutionConfig()


def _merge_dataclass(obj, overrides: dict, prefix: str = ""):
    for k, v in overrides.items():
        target_key = f"{prefix}{k}" if prefix else k
        if hasattr(obj, target_key):
            setattr(obj, target_key, v)
