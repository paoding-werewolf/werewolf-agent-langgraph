import os
import sys
import tempfile
import asyncio
from datetime import datetime
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_db.name}"
os.environ["PROVIDER_PUBLIC_HTTP_URL"] = "http://provider.test:8083"
os.environ["PROVIDER_PUBLIC_WS_URL"] = "ws://provider.test:8082"

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import evolution.db as evolution_db

_test_engine = create_engine(os.environ["DATABASE_URL"])
evolution_db.engine = _test_engine
evolution_db.SessionLocal = sessionmaker(bind=_test_engine, autoflush=False, autocommit=False)

from evolution.conjugate_agent import (
    _parse_json_object,
    backfill_conjugate_agent_identities,
    maybe_create_conjugate_agent,
)
from evolution.db import Base, engine, get_session
from evolution.models import ConjugateAgent, EvolutionGameArchive, EvolutionSkill, EvolutionSkillVersion
from main_ws import _build_versions_used, _list_provider_agents, _process_game_over, _process_init, store
from memory.game_archive import save_game


def setup_module():
    _assert_sqlite_test_db()
    Base.metadata.create_all(bind=engine)


def setup_function():
    _assert_sqlite_test_db()
    with store._lock:
        store._sessions.clear()
    session = get_session()
    try:
        for model in (EvolutionGameArchive, ConjugateAgent, EvolutionSkillVersion, EvolutionSkill):
            session.query(model).delete()
        session.commit()
    finally:
        session.close()


@pytest.fixture(autouse=True)
def mock_agent_identity_llm(monkeypatch):
    def fake_llm_identity(facts):
        return {
            "agent_name": str(facts.get("fallback_agent_name") or "影刃"),
            "changelog": str(facts.get("fallback_changelog") or "策略发生晋升。"),
            "lore": "测试环境生成的共轭 Agent 人设。",
        }

    monkeypatch.setattr("evolution.conjugate_agent._generate_agent_identity_with_llm", fake_llm_identity)


def _assert_sqlite_test_db():
    assert engine.url.get_backend_name() == "sqlite"
    assert str(engine.url.database).endswith(".db")


def _add_skill(session, name: str, role: str, current_default: str, versions: list[str]):
    skill = EvolutionSkill(
        skill_name=name,
        role=role,
        description=f"{name} strategy",
        tags_json=[role],
        current_default=current_default,
    )
    session.add(skill)
    session.flush()
    for version in versions:
        session.add(EvolutionSkillVersion(
            skill_id=skill.id,
            version=version,
            status="active" if version == current_default else "candidate",
            content_markdown=f"{name} {version}",
        ))
    return skill


def _seed_initial_skills():
    session = get_session()
    try:
        _add_skill(session, "wolf-logic", "wolf", "v1", ["v1", "v2"])
        _add_skill(session, "seer-logic", "seer", "v1", ["v1"])
        _add_skill(session, "common-logic", "common", "v1", ["v1"])
        session.commit()
    finally:
        session.close()


def _promote_wolf_logic():
    session = get_session()
    try:
        skill = session.query(EvolutionSkill).filter_by(skill_name="wolf-logic").first()
        candidate = session.query(EvolutionSkillVersion).filter_by(
            skill_id=skill.id,
            version="v2",
        ).first()
        previous = skill.current_default
        skill.current_default = "v2"
        candidate.status = "active"
        agent = maybe_create_conjugate_agent(session, skill, previous, candidate)
        session.commit()
        return agent.id
    finally:
        session.close()


def test_identity_parser_handles_fenced_json():
    parsed = _parse_json_object(
        """```json
{
  "agent_name": "霜牙",
  "changelog": "wolf-logic 从 v1 到 v2。",
  "lore": "她从旧影中醒来。"
}
```"""
    )

    assert parsed["agent_name"] == "霜牙"
    assert "wolf-logic" in parsed["changelog"]
    assert "旧影" in parsed["lore"]


def test_identity_parser_recovers_unescaped_quotes_in_fields():
    parsed = _parse_json_object(
        """
模型输出如下：
{
  "agent_name": "玄棘",
  "changelog": "wolf-logic 从 v1 晋升到 v2，新增 "夜间击杀" 判断，移除旧直觉。",
  "lore": "她学会在 "伪装" 的裂缝中追踪猎物。"
}
"""
    )

    assert parsed["agent_name"] == "玄棘"
    assert '新增 "夜间击杀" 判断' in parsed["changelog"]
    assert '"伪装"' in parsed["lore"]


def test_identity_parser_repairs_loose_json():
    parsed = _parse_json_object(
        """
{
  agent_name: "雾裁",
  changelog: "修复了狼人夜间击杀判断",
  lore: "雾裁在旧档案的裂隙中醒来",
}
"""
    )

    assert parsed["agent_name"] == "雾裁"
    assert "狼人夜间击杀" in parsed["changelog"]
    assert "旧档案" in parsed["lore"]


def test_promoted_conjugate_agent_identity_comes_from_llm(monkeypatch):
    _seed_initial_skills()
    _initial_external_agent_id()

    def fake_identity(facts):
        assert facts["trigger_skill_name"] == "wolf-logic"
        assert facts["previous_version"] == "v1"
        assert facts["new_version"] == "v2"
        return {
            "agent_name": "霜牙",
            "changelog": "wolf-logic 从 v1 晋升到 v2，夜间击杀策略更重视发言矛盾和身份伪装成本。",
            "lore": "霜牙继承了初代的夜色本能，却把旧日的直觉磨成冷白的齿锋。它不再只追逐暴露的猎物，而是在谎言的边缘等待伪装者露出呼吸。",
        }

    monkeypatch.setattr("evolution.conjugate_agent.generate_agent_identity", fake_identity)
    promoted_id = _promote_wolf_logic()

    session = get_session()
    try:
        agent = session.get(ConjugateAgent, promoted_id)
        assert agent.agent_name == "霜牙"
        assert "wolf-logic 从 v1 晋升到 v2" in agent.changelog
        assert "继承了初代" in agent.lore
    finally:
        session.close()


def test_initial_conjugate_agent_identity_comes_from_llm(monkeypatch):
    _seed_initial_skills()

    def fake_identity(facts):
        assert facts["event_type"] == "genesis"
        assert facts["trigger_skill_name"] == "initial-skill-library"
        return {
            "agent_name": "影刃",
            "changelog": "初代共轭快照收束 wolf-logic、seer-logic 与 common-logic，建立夜战、查验和通用发言的基础策略人格。",
            "lore": "影刃在第一夜之前醒来。它没有前代，却把狼人的低语、预言家的凝视与村民的犹疑压进同一枚指纹，像一柄尚未出鞘的黑刃等待第一场审判。",
        }

    monkeypatch.setattr("evolution.conjugate_agent.generate_agent_identity", fake_identity)
    agent = _list_provider_agents()[0]

    assert agent["agent_name"] == "001影刃"
    assert agent["metadata"]["base_agent_name"] == "影刃"
    assert agent["metadata"]["display_name"] == "001影刃"
    assert agent["metadata"]["lineage_index"] == 1
    assert agent["metadata"]["lineage_serial"] == "001"
    assert "初代共轭快照" in agent["metadata"]["changelog"]
    assert "第一夜之前醒来" in agent["metadata"]["lore"]


def test_backfill_legacy_identity(monkeypatch):
    _seed_initial_skills()
    session = get_session()
    try:
        session.add(ConjugateAgent(
            fingerprint="legacy-fingerprint",
            agent_name="影刃-初纪",
            avatar_seed="legacy-seed",
            born_at=datetime.utcnow(),
            skill_versions_json={
                "common-logic": "v1",
                "seer-logic": "v1",
                "wolf-logic": "v1",
            },
            changelog="Genesis: initial skill library snapshot",
            lore="",
        ))
        session.commit()
    finally:
        session.close()

    def fake_identity(facts):
        assert facts["event_type"] == "genesis_backfill"
        return {
            "agent_name": "夜裁",
            "changelog": "旧初代快照已补齐为自然语言变更说明，保留三项基础 skill 的共轭组合。",
            "lore": "夜裁从旧档案中苏醒，补回了遗失的人格与宣发叙事。",
        }

    monkeypatch.setattr("evolution.conjugate_agent.generate_agent_identity", fake_identity)
    session = get_session()
    try:
        assert backfill_conjugate_agent_identities(session) == 1
        session.commit()
    finally:
        session.close()

    agent = _list_provider_agents()[0]

    assert agent["agent_name"] == "001夜裁"
    assert agent["metadata"]["base_agent_name"] == "夜裁"
    assert "旧初代快照" in agent["metadata"]["changelog"]
    assert "旧档案中苏醒" in agent["metadata"]["lore"]


def test_legacy_identity_backfill_fallback_does_not_retry(monkeypatch):
    _seed_initial_skills()
    session = get_session()
    try:
        session.add(ConjugateAgent(
            fingerprint="legacy-fallback-fingerprint",
            agent_name="影刃-初纪",
            avatar_seed="legacy-seed",
            born_at=datetime.utcnow(),
            skill_versions_json={
                "common-logic": "v1",
                "seer-logic": "v1",
                "wolf-logic": "v1",
            },
            changelog="Genesis: initial skill library snapshot",
            lore="",
        ))
        session.commit()
    finally:
        session.close()

    calls = {"count": 0}

    def fail_identity(_facts):
        calls["count"] += 1
        raise RuntimeError("llm unavailable")

    monkeypatch.setattr("evolution.conjugate_agent._generate_agent_identity_with_llm", fail_identity)

    session = get_session()
    try:
        assert backfill_conjugate_agent_identities(session) == 1
        session.commit()
    finally:
        session.close()

    session = get_session()
    try:
        assert backfill_conjugate_agent_identities(session) == 0
        session.commit()
    finally:
        session.close()

    first = _list_provider_agents()[0]
    second = _list_provider_agents()[0]

    assert calls["count"] == 1
    assert first["agent_name"] == "001影刃-初纪"
    assert first["metadata"]["base_agent_name"] == "影刃-初纪"
    assert first["metadata"]["lore"]
    assert second["metadata"]["lore"] == first["metadata"]["lore"]


def test_promoted_conjugate_agent_uses_fallback_when_llm_fails(monkeypatch):
    _seed_initial_skills()
    _initial_external_agent_id()

    def fail_identity(_facts):
        raise RuntimeError("llm unavailable")

    monkeypatch.setattr("evolution.conjugate_agent._generate_agent_identity_with_llm", fail_identity)
    promoted_id = _promote_wolf_logic()

    session = get_session()
    try:
        agent = session.get(ConjugateAgent, promoted_id)
        assert agent.agent_name
        assert "wolf-logic 从 v1 晋升到 v2" in agent.changelog
        assert agent.lore == ""
    finally:
        session.close()


def _initial_external_agent_id() -> str:
    """返回初始共轭 Agent 的 external_agent_id（跳过 latest:evolution 固定条目）。"""
    agents = _list_provider_agents()
    for agent in agents:
        if agent["external_agent_id"].startswith("agent:"):
            return agent["external_agent_id"]
    return agents[0]["external_agent_id"]


def test_list_provider_agents_exposes_initial_conjugate_snapshot():
    _seed_initial_skills()

    agents = _list_provider_agents()

    # 首项是 latest:evolution 固定条目，第二项是共轭 Agent
    assert len(agents) == 2
    fixed, conjugate = agents[0], agents[1]

    assert fixed["external_agent_id"] == "latest:evolution"
    assert fixed["metadata"]["mode"] == "latest-evolution"
    assert fixed["metadata"]["is_latest"] is True
    assert fixed["agent_name"] == "001影刃-初纪"
    assert fixed["metadata"]["base_agent_name"] == "影刃-初纪"
    assert fixed["metadata"]["display_name"] == "001影刃-初纪"
    assert fixed["metadata"]["lineage_index"] == 1
    assert fixed["metadata"]["lineage_serial"] == "001"

    assert conjugate["external_agent_id"].startswith("agent:")
    assert conjugate["agent_name"] == "001影刃-初纪"
    assert conjugate["client_url"] == "ws://provider.test:8082"
    assert conjugate["metadata"]["mode"] == "conjugate"
    assert conjugate["metadata"]["is_latest"] is True
    assert conjugate["metadata"]["base_agent_name"] == "影刃-初纪"
    assert conjugate["metadata"]["display_name"] == "001影刃-初纪"
    assert conjugate["metadata"]["lineage_index"] == 1
    assert conjugate["metadata"]["lineage_serial"] == "001"
    assert conjugate["metadata"]["skill_versions"] == {
        "common-logic": "v1",
        "seer-logic": "v1",
        "wolf-logic": "v1",
    }
    assert conjugate["metadata"]["changelog"]
    assert conjugate["metadata"]["lore"]


def test_list_provider_agents_exposes_latest_and_historical_conjugates():
    _seed_initial_skills()
    # 拿到初始共轭 Agent 的 ID（列表第二项）
    initial_id = _list_provider_agents()[1]["external_agent_id"]
    promoted_id = _promote_wolf_logic()

    agents = _list_provider_agents()

    agent_ids = [agent["external_agent_id"] for agent in agents]
    assert agent_ids == ["latest:evolution", f"agent:{promoted_id}", initial_id]
    assert agents[1]["metadata"]["lineage_index"] == 2
    assert agents[1]["metadata"]["lineage_serial"] == "002"
    assert agents[1]["agent_name"].startswith("002")
    assert agents[2]["metadata"]["lineage_index"] == 1
    assert agents[2]["metadata"]["lineage_serial"] == "001"
    assert agents[2]["agent_name"].startswith("001")
    assert agents[1]["metadata"]["is_latest"] is True
    assert agents[1]["metadata"]["skill_versions"]["wolf-logic"] == "v2"
    assert agents[2]["metadata"]["is_latest"] is False
    assert agents[2]["metadata"]["skill_versions"]["wolf-logic"] == "v1"


def test_historical_conjugate_agent_uses_frozen_snapshot():
    _seed_initial_skills()
    # 先拿到初始共轭 Agent 的 ID（列表第二项，第一项是 latest:evolution）
    initial_id = _list_provider_agents()[1]["external_agent_id"]
    _promote_wolf_logic()

    versions = _build_versions_used("wolf", initial_id)

    assert versions == {
        "common-logic": "v1",
        "seer-logic": "v1",
        "wolf-logic": "v1",
    }


def test_latest_conjugate_agent_uses_live_version_competition(monkeypatch):
    _seed_initial_skills()
    _list_provider_agents()
    latest_id = _promote_wolf_logic()

    def fake_get_version_for_game(skill_name):
        return f"live-{skill_name}"

    monkeypatch.setattr(
        "evolution.skill_loader.SkillLoader.get_version_for_game",
        lambda self, skill_name: fake_get_version_for_game(skill_name),
    )

    versions = _build_versions_used("wolf", f"agent:{latest_id}")

    assert versions == {
        "common-logic": "live-common-logic",
        "wolf-logic": "live-wolf-logic",
    }


def test_process_init_persists_versions_and_strategy_names(monkeypatch):
    _seed_initial_skills()

    monkeypatch.setattr(
        "main_ws._build_versions_used",
        lambda role, external_agent_id=None: {"wolf-logic": "v1", "common-logic": "v1"},
    )

    session_id, _resp = asyncio.run(_process_init("1_wolf", "wolf", [], "req-1"))
    state = store.get(session_id)

    assert state["versions_used"] == {"wolf-logic": "v1", "common-logic": "v1"}
    assert state["strategies_used"] == ["wolf-logic", "common-logic"]


def test_save_game_archives_versions_used():
    save_game(
        game_id="game-1",
        my_role="wolf",
        result="won",
        day_count=3,
        scene_tags={"role": "wolf"},
        reflection_report="report",
        full_trace="trace",
        strategies_used=["wolf-logic"],
        versions_used={"wolf-logic": "v2"},
    )

    session = get_session()
    try:
        record = session.query(EvolutionGameArchive).filter_by(game_id="game-1").first()
        assert record.payload_json["strategies_used"] == ["wolf-logic"]
        assert record.payload_json["versions_used"] == {"wolf-logic": "v2"}
    finally:
        session.close()


def test_game_over_skips_evolution_for_historical_conjugate(monkeypatch):
    _seed_initial_skills()
    # 初始共轭 Agent ID（列表第二项）
    initial_id = _list_provider_agents()[1]["external_agent_id"]
    _promote_wolf_logic()
    state = {
        "external_agent_id": initial_id,
        "room_id": "room-1",
        "my_role": "wolf",
        "me_id": "1",
        "events": [],
        "players": {},
        "versions_used": {"wolf-logic": "v1"},
    }
    store.create("session-1", state)

    called = False

    async def fake_pipeline(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr("main_ws._run_post_game_pipeline", fake_pipeline)

    resp = asyncio.run(
        _process_game_over(
            "session-1",
            "req-1",
            "won",
            "wolf",
            {},
            False,
            "room-1",
            "wolf",
        )
    )

    assert resp["skip_evolution"] is True
    assert resp["reason"] == "frozen_conjugate_agent"
    assert called is False


def test_list_provider_agents_exposes_latest_evolution_fixed_id():
    _seed_initial_skills()

    agents = _list_provider_agents()

    fixed = agents[0]
    assert fixed["external_agent_id"] == "latest:evolution"
    assert fixed["agent_name"] == "001影刃-初纪"
    assert fixed["metadata"]["mode"] == "latest-evolution"
    assert fixed["metadata"]["is_latest"] is True
    assert fixed["metadata"]["base_agent_name"] == "影刃-初纪"
    assert fixed["metadata"]["lineage_index"] == 1
    assert fixed["metadata"]["lineage_serial"] == "001"
    assert fixed["client_type"] == "ws"
    assert fixed["client_url"] == "ws://provider.test:8082"


def test_latest_evolution_id_resolves_to_live_version_competition(monkeypatch):
    _seed_initial_skills()
    _list_provider_agents()  # ensure initial conjugate agent exists
    _promote_wolf_logic()

    def fake_get_version_for_game(skill_name):
        return f"live-{skill_name}"

    monkeypatch.setattr(
        "evolution.skill_loader.SkillLoader.get_version_for_game",
        lambda self, skill_name: fake_get_version_for_game(skill_name),
    )

    versions = _build_versions_used("wolf", "latest:evolution")

    assert versions == {
        "common-logic": "live-common-logic",
        "wolf-logic": "live-wolf-logic",
    }


def test_game_over_runs_evolution_for_latest_evolution_id(monkeypatch):
    _seed_initial_skills()
    _list_provider_agents()  # ensure initial conjugate agent exists
    _promote_wolf_logic()

    state = {
        "external_agent_id": "latest:evolution",
        "room_id": "room-1",
        "my_role": "wolf",
        "me_id": "1",
        "events": [],
        "players": {},
        "versions_used": {"wolf-logic": "v2"},
    }
    store.create("session-le", state)

    called = False

    async def fake_pipeline(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr("main_ws._run_post_game_pipeline", fake_pipeline)

    resp = asyncio.run(
        _process_game_over(
            "session-le",
            "req-1",
            "won",
            "wolf",
            {},
            False,
            "room-1",
            "wolf",
        )
    )

    assert "skip_evolution" not in resp or resp.get("skip_evolution") is not True
    assert called is True
