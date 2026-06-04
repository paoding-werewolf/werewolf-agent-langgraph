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
from main_ws import _list_provider_agents


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
        session.commit()
    finally:
        session.close()

    agents = _list_provider_agents()

    default_agent = next(a for a in agents if a["external_agent_id"] == "default:common")
    candidate_agent = next(a for a in agents if a["external_agent_id"] == "skill:wolf-logic:v2:wolf")

    assert default_agent["client_url"] == "ws://provider.test:8082"
    assert candidate_agent["version"] == "v2"
    assert candidate_agent["metadata"]["skill_name"] == "wolf-logic"
