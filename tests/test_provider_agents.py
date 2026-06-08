import os
import sys
import tempfile
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_db.name}"
os.environ["PROVIDER_PUBLIC_HTTP_URL"] = "http://provider.test:8083"
os.environ["PROVIDER_PUBLIC_WS_URL"] = "ws://provider.test:8082"

from evolution.conjugate_agent import maybe_create_conjugate_agent
from evolution.db import Base, engine, get_session
from evolution.models import ConjugateAgent, EvolutionSkill, EvolutionSkillVersion
from main_ws import _build_versions_used, _list_provider_agents, _process_game_over, store


def setup_module():
    Base.metadata.create_all(bind=engine)


def setup_function():
    with store._lock:
        store._sessions.clear()
    session = get_session()
    try:
        for model in (ConjugateAgent, EvolutionSkillVersion, EvolutionSkill):
            session.query(model).delete()
        session.commit()
    finally:
        session.close()


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


def _initial_external_agent_id() -> str:
    return _list_provider_agents()[0]["external_agent_id"]


def test_list_provider_agents_exposes_initial_conjugate_snapshot():
    _seed_initial_skills()

    agents = _list_provider_agents()

    assert len(agents) == 1
    agent = agents[0]
    assert agent["external_agent_id"].startswith("agent:")
    assert agent["agent_name"]
    assert agent["client_url"] == "ws://provider.test:8082"
    assert agent["metadata"]["mode"] == "conjugate"
    assert agent["metadata"]["is_latest"] is True
    assert agent["metadata"]["skill_versions"] == {
        "common-logic": "v1",
        "seer-logic": "v1",
        "wolf-logic": "v1",
    }
    assert agent["metadata"]["changelog"] == "Genesis: initial skill library snapshot"


def test_list_provider_agents_exposes_latest_and_historical_conjugates():
    _seed_initial_skills()
    initial_id = _initial_external_agent_id()
    promoted_id = _promote_wolf_logic()

    agents = _list_provider_agents()

    assert [agent["external_agent_id"] for agent in agents] == [f"agent:{promoted_id}", initial_id]
    assert agents[0]["metadata"]["is_latest"] is True
    assert agents[0]["metadata"]["skill_versions"]["wolf-logic"] == "v2"
    assert agents[1]["metadata"]["is_latest"] is False
    assert agents[1]["metadata"]["skill_versions"]["wolf-logic"] == "v1"


def test_historical_conjugate_agent_uses_frozen_snapshot():
    _seed_initial_skills()
    initial_id = _initial_external_agent_id()
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


def test_game_over_skips_evolution_for_historical_conjugate(monkeypatch):
    _seed_initial_skills()
    initial_id = _initial_external_agent_id()
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
