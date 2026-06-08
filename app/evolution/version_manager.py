"""evolution/version_manager.py — 策略版本管理门面（MySQL 持久化）

封装 SkillLoader 中版本操作的高层 API，供 confirmation.py 和 API 层调用。
"""
from typing import Dict, List, Optional, Any

from evolution.config import EvolutionConfig
from evolution.skill_loader import SkillLoader


class VersionManager:
    """策略版本管理门面。委托 SkillLoader 完成实际操作。"""

    def __init__(self, cfg: EvolutionConfig):
        self.cfg = cfg
        self.loader = SkillLoader(cfg)

    def create_new_version(self, skill_name: str, content: str,
                           source: str = "debounced_update",
                           trigger_cluster: str = "",
                           role: str = "") -> str:
        return self.loader.create_new_version(skill_name, content, source, trigger_cluster, role)

    def rollback(self, skill_name: str, target_version: str) -> bool:
        return self.loader.rollback(skill_name, target_version)

    def pin_version(self, skill_name: str, version: str, pinned: bool) -> bool:
        return self.loader.pin_version(skill_name, version, pinned)

    def delete_version(self, skill_name: str, version: str) -> bool:
        return self.loader.delete_version(skill_name, version)

    def load_skill_full(self, skill_name: str, version: str = None) -> str:
        return self.loader.load_skill_full(skill_name, version) or ""

    def get_version_for_game(self, skill_name: str) -> str:
        return self.loader.get_version_for_game(skill_name)

    def record_usage(self, skill_name: str, version: str, won: bool):
        self.loader.record_version_usage(skill_name, version, won)

    def format_skills_for_prompt(self, my_role: str, phase: str,
                                  versions_used: Optional[dict] = None) -> str:
        """组装注入 prompt 的策略文本（Layer 1 索引 + Layer 2 全文）。

        如果 versions_used 非空，按指定版本加载；否则走 current_default。
        """
        index_text = self.loader.format_index_for_prompt(my_role)
        if versions_used:
            full_text = self.loader.load_skills_with_versions(my_role, phase, versions_used)
        else:
            full_text = self.loader.load_skills_for_context(my_role, phase)

        parts = []
        if index_text:
            parts.append(index_text)
        if full_text:
            parts.append("## Active Strategy Details")
            parts.append(full_text)

        return "\n\n".join(parts)
