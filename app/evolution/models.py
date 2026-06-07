"""evolution/models.py — 自进化 MySQL ORM 模型（与 init-mysql.sql 对齐）"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import DATETIME, Integer, JSON, String, Boolean, Text
from sqlalchemy.orm import Mapped, mapped_column

from evolution.db import Base


def _utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class EvolutionSkill(Base):
    __tablename__ = "evolution_skills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    skill_name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tags_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    current_default: Mapped[str] = mapped_column(String(32), nullable=False)
    skill_games_played: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skill_wins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skill_win_rate: Mapped[float] = mapped_column(default=0.0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME, default=_utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DATETIME, default=_utc_now, nullable=False)


class EvolutionSkillVersion(Base):
    __tablename__ = "evolution_skill_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    skill_id: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="candidate", nullable=False)
    source: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    trigger_cluster_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    content_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    games_played: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    wins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    win_rate: Mapped[float] = mapped_column(default=0.0, nullable=False)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DATETIME, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DATETIME, default=_utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DATETIME, default=_utc_now, nullable=False)


class EvolutionBufferItem(Base):
    __tablename__ = "evolution_buffer_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_type: Mapped[str] = mapped_column(String(16), nullable=False)
    item_key: Mapped[str] = mapped_column(String(128), nullable=False)
    cluster_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    target_skill_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    suggestion_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    avg_causal_strength: Mapped[float] = mapped_column(default=0.0, nullable=False)
    consistency_rate: Mapped[float] = mapped_column(default=0.0, nullable=False)
    scene_tags_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    preview_texts_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    source_path: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DATETIME, default=_utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DATETIME, default=_utc_now, nullable=False)


class EvolutionStrategyGap(Base):
    __tablename__ = "evolution_strategy_gaps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scene_description: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    gap_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME, default=_utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DATETIME, default=_utc_now, nullable=False)


class EvolutionGameArchive(Base):
    __tablename__ = "evolution_game_archive"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    game_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    room_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    my_role: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    result: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    day_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    has_builtin_ai: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME, default=_utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DATETIME, default=_utc_now, nullable=False)


class EvolutionRuntimeState(Base):
    __tablename__ = "evolution_runtime_state"

    state_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME, default=_utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DATETIME, default=_utc_now, nullable=False)


class EvolutionPipelineLog(Base):
    __tablename__ = "evolution_pipeline_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    logger_name: Mapped[str] = mapped_column(String(64), nullable=False)
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    session_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DATETIME, default=_utc_now, nullable=False)
