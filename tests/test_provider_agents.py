import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_db.name}"
os.environ["PROVIDER_PUBLIC_HTTP_URL"] = "http://provider.test:8083"
os.environ["PROVIDER_PUBLIC_WS_URL"] = "ws://provider.test:8082"

from evolution.db import Base, engine, get_session
from evolution.models import EvolutionSkill, EvolutionSkillVersion
from main_ws import _build_versions_used, _list_provider_agents


def setup_module():
    Base.metadata.create_all(bind=engine)


def test_list_provider_agents_includes_candidate_versions():
    session = get_session()
    try:
        skill = EvolutionSkill(
            skill_name="wolf-logic",
            role="wolf",
            description="wolf strategy",
            tags_json=["wolf"],
            current_default="v1",
        )
        session.add(skill)
        session.flush()
        session.add_all([
            EvolutionSkillVersion(
                skill_id=skill.id,
                version="v1",
                status="active",
                content_markdown="base",
            ),
            EvolutionSkillVersion(
                skill_id=skill.id,
                version="v2",
                status="candidate",
                content_markdown="candidate",
            ),
        ])
        good_skill = EvolutionSkill(
            skill_name="seer-logic",
            role="seer",
            description="seer strategy",
            tags_json=["seer"],
            current_default="v1",
        )
        session.add(good_skill)
        session.flush()
        session.add(EvolutionSkillVersion(
            skill_id=good_skill.id,
            version="v1",
            status="active",
            content_markdown="seer base",
        ))
        wolf_king_skill = EvolutionSkill(
            skill_name="wolf-king-logic",
            role="wolf_king",
            description="wolf king strategy",
            tags_json=["wolf"],
            current_default="v1",
        )
        session.add(wolf_king_skill)
        session.flush()
        session.add(EvolutionSkillVersion(
            skill_id=wolf_king_skill.id,
            version="v1",
            status="active",
            content_markdown="wolf king base",
        ))
        session.commit()
    finally:
        session.close()

    agents = _list_provider_agents()

    default_agent = next(a for a in agents if a["external_agent_id"] == "default:common")
    candidate_agent = next(a for a in agents if a["external_agent_id"] == "skill:wolf-logic:v2:wolf")
    wolf_king_agent = next(a for a in agents if a["external_agent_id"] == "skill:wolf-king-logic:v1:wolf")
    role_agent = next(a for a in agents if a["external_agent_id"] == "role:wolf:v1")
    role_v2_agent = next(a for a in agents if a["external_agent_id"] == "role:wolf:v2")
    camp_agent = next(a for a in agents if a["external_agent_id"] == "camp:good:v1")

    assert default_agent["client_url"] == "ws://provider.test:8082"
    assert candidate_agent["version"] == "v2"
    assert candidate_agent["metadata"]["skill_name"] == "wolf-logic"
    assert wolf_king_agent["metadata"]["role_scope"] == "wolf"
    assert role_agent["metadata"]["mode"] == "role_lock"
    assert role_v2_agent["metadata"]["role_scope"] == "wolf"
    assert camp_agent["metadata"]["mode"] == "camp_lock"


def test_role_lock_external_agent_id_freezes_role_versions():
    versions = _build_versions_used("wolf", "role:wolf:v1")

    assert versions["wolf-logic"] == "v1"
    assert versions["wolf-king-logic"] == "v1"


def test_role_lock_external_agent_id_uses_best_available_version_at_or_below_target():
    versions = _build_versions_used("wolf", "role:wolf:v2")

    assert versions["wolf-logic"] == "v2"
    assert versions["wolf-king-logic"] == "v1"


def test_role_lock_external_agent_id_does_not_apply_to_other_roles():
    versions = _build_versions_used("villager", "role:wolf:v1")

    assert "wolf-logic" not in versions


def test_camp_lock_external_agent_id_freezes_good_versions():
    versions = _build_versions_used("seer", "camp:good:v1")

    assert versions["seer-logic"] == "v1"
    assert "wolf-logic" not in versions
