"""test_gepa_balanced_select.py — 角色均衡采样逻辑单元测试

不依赖数据库，构造 mock 数据验证 GEPA._balanced_select() 的角色均衡行为。

用法：
  cd werewolf-agent-langgraph
  PYTHONPATH=app python3 test_gepa_balanced_select.py
"""
import sys
sys.path.insert(0, "app")

from evolution.gepa import GEPA
from evolution.config import EvolutionConfig


def make_individual(role: str, skill_name: str, version: str,
                    status: str = "active", win_rate: float = 0.5,
                    games_played: int = 10) -> dict:
    """构造一个 mock 个体。"""
    return {
        "key": f"{skill_name}:{version}",
        "skill_name": skill_name,
        "skill_id": 1,
        "version": version,
        "content": f"# {skill_name} {version}",
        "games_played": games_played,
        "wins": int(win_rate * games_played),
        "win_rate": win_rate,
        "skill_games_played": games_played,
        "skill_win_rate": win_rate,
        "source": "bundled",
        "role": role,
        "status": status,
    }


def test_basic_5_roles():
    """5 角色 × 多版本，population_size=8。"""
    individuals = []
    # guard: 10 candidates
    for i in range(10):
        individuals.append(make_individual("guard", "guard-priority", f"v{i+10}",
                                           status="candidate", win_rate=0.0, games_played=0))
    # guard: 1 active (best)
    individuals.append(make_individual("guard", "guard-priority", "v2",
                                       status="active", win_rate=0.55, games_played=20))
    # wolf: 5 versions (1 active, 4 candidate)
    individuals.append(make_individual("wolf", "wolf-deep-undercover", "v2",
                                       status="active", win_rate=0.4, games_played=15))
    for i in range(4):
        individuals.append(make_individual("wolf", "wolf-deep-undercover", f"v{i+3}",
                                           status="candidate", win_rate=0.0, games_played=0))
    # seer: 2 versions
    individuals.append(make_individual("seer", "seer-day1-target", "v3",
                                       status="active", win_rate=0.35, games_played=10))
    individuals.append(make_individual("seer", "seer-day1-target", "v4",
                                       status="candidate", win_rate=0.0, games_played=0))
    # witch: 1 version
    individuals.append(make_individual("witch", "witch-info-verify", "v3",
                                       status="active", win_rate=0.3, games_played=8))
    # hunter: 1 version
    individuals.append(make_individual("hunter", "hunter-identity-reveal", "v1",
                                       status="active", win_rate=0.25, games_played=5))

    result = GEPA._balanced_select(individuals, 8)

    print(f"Test basic_5_roles: selected {len(result)} individuals")
    role_counts = {}
    for ind in result:
        r = ind["role"]
        role_counts[r] = role_counts.get(r, 0) + 1
    print(f"  Role distribution: {role_counts}")

    # 验证每角色至少1个
    assert len(result) == 8, f"Expected 8, got {len(result)}"
    for role in ["guard", "wolf", "seer", "witch", "hunter"]:
        assert role in role_counts, f"Role {role} missing from population"
        assert role_counts[role] >= 1, f"Role {role} has 0 representatives"
    # guard 的 active 版本应该被选中（不是 candidate）
    guard_inds = [ind for ind in result if ind["role"] == "guard"]
    assert any(ind["status"] == "active" for ind in guard_inds), "Guard active version not selected"
    print("  ✅ PASSED")


def test_roles_exceed_size():
    """6 角色, population_size=4。只取 win_rate 最高的4个角色。"""
    individuals = [
        make_individual("guard", "guard-p", "v1", status="active", win_rate=0.6, games_played=20),
        make_individual("wolf", "wolf-d", "v1", status="active", win_rate=0.5, games_played=15),
        make_individual("seer", "seer-d", "v1", status="active", win_rate=0.4, games_played=10),
        make_individual("witch", "witch-i", "v1", status="active", win_rate=0.3, games_played=8),
        make_individual("hunter", "hunter-i", "v1", status="active", win_rate=0.2, games_played=5),
        make_individual("villager", "villager-c", "v1", status="active", win_rate=0.1, games_played=3),
    ]

    result = GEPA._balanced_select(individuals, 4)

    print(f"Test roles_exceed_size: selected {len(result)} individuals")
    role_counts = {}
    for ind in result:
        r = ind["role"]
        role_counts[r] = role_counts.get(r, 0) + 1
    print(f"  Role distribution: {role_counts}")

    assert len(result) == 4, f"Expected 4, got {len(result)}"
    # top 4 by win_rate: guard, wolf, seer, witch
    assert "guard" in role_counts, "guard should be selected"
    assert "wolf" in role_counts, "wolf should be selected"
    assert "seer" in role_counts, "seer should be selected"
    assert "witch" in role_counts, "witch should be selected"
    # hunter and villager should NOT be selected (win_rate too low)
    assert "hunter" not in role_counts, "hunter should not be selected (win_rate too low)"
    assert "villager" not in role_counts, "villager should not be selected (win_rate too low)"
    print("  ✅ PASSED")


def test_all_candidate():
    """全部版本都是 candidate（无实战数据）。"""
    individuals = []
    for role in ["guard", "wolf", "seer"]:
        for i in range(3):
            individuals.append(make_individual(role, f"{role}-skill", f"v{i}",
                                               status="candidate", win_rate=0.0, games_played=0))

    result = GEPA._balanced_select(individuals, 6)

    print(f"Test all_candidate: selected {len(result)} individuals")
    role_counts = {}
    for ind in result:
        r = ind["role"]
        role_counts[r] = role_counts.get(r, 0) + 1
    print(f"  Role distribution: {role_counts}")

    assert len(result) == 6, f"Expected 6, got {len(result)}"
    for role in ["guard", "wolf", "seer"]:
        assert role in role_counts, f"Role {role} should still be represented"
    print("  ✅ PASSED")


def test_guard_flood():
    """守卫洪水：100 个 candidate，其他角色各1个 active。"""
    individuals = []
    for i in range(100):
        individuals.append(make_individual("guard", "guard-p", f"v{i+3}",
                                           status="candidate", win_rate=0.0, games_played=0))
    individuals.append(make_individual("guard", "guard-p", "v2",
                                       status="active", win_rate=0.55, games_played=20))

    for role, skill, wr, gp in [
        ("wolf", "wolf-d", 0.4, 15),
        ("seer", "seer-d", 0.35, 10),
        ("witch", "witch-i", 0.3, 8),
        ("hunter", "hunter-i", 0.25, 5),
        ("villager", "villager-c", 0.2, 3),
    ]:
        individuals.append(make_individual(role, skill, "v1",
                                           status="active", win_rate=wr, games_played=gp))

    result = GEPA._balanced_select(individuals, 8)

    print(f"Test guard_flood: selected {len(result)} individuals")
    role_counts = {}
    for ind in result:
        r = ind["role"]
        role_counts[r] = role_counts.get(r, 0) + 1
    print(f"  Role distribution: {role_counts}")

    # 验证每角色至少1个
    assert len(result) == 8
    for role in ["guard", "wolf", "seer", "witch", "hunter", "villager"]:
        assert role in role_counts, f"Role {role} missing"
    # 验证非守卫角色的 active 版本被选中
    for ind in result:
        if ind["role"] != "guard":
            assert ind["status"] == "active", f"Non-guard {ind['role']} should be active, got {ind['status']}"
    # guard 的 active 版本应该被选中
    guard_inds = [ind for ind in result if ind["role"] == "guard"]
    assert any(ind["status"] == "active" for ind in guard_inds), "Guard active version not selected"
    # guard 不应该超过3个名额（6个其他角色各1 + guard最多2-3个）
    assert role_counts.get("guard", 0) <= 3, f"Guard should not dominate: {role_counts['guard']} out of 8"
    print("  ✅ PASSED")


def test_empty_input():
    """空输入。"""
    result = GEPA._balanced_select([], 8)
    assert result == [], f"Expected [], got {result}"
    print("Test empty_input: ✅ PASSED")


def test_single_role():
    """只有一个角色的版本。"""
    individuals = [make_individual("guard", "guard-p", "v2",
                                   status="active", win_rate=0.5, games_played=10)]
    for i in range(5):
        individuals.append(make_individual("guard", "guard-p", f"v{i+3}",
                                           status="candidate", win_rate=0.0, games_played=0))

    result = GEPA._balanced_select(individuals, 4)
    print(f"Test single_role: selected {len(result)} individuals")
    assert len(result) == 4, f"Expected 4, got {len(result)}"
    # all should be guard
    for ind in result:
        assert ind["role"] == "guard"
    # active version should be first
    assert result[0]["status"] == "active"
    print("  ✅ PASSED")


def test_status_priority_ordering():
    """验证状态优先级排序：active > stale > superseded > candidate。"""
    individuals = [
        make_individual("seer", "seer-d", "v_candidate", status="candidate", win_rate=0.8),
        make_individual("seer", "seer-d", "v_superseded", status="superseded", win_rate=0.7),
        make_individual("seer", "seer-d", "v_stale", status="stale", win_rate=0.6),
        make_individual("seer", "seer-d", "v_active", status="active", win_rate=0.5),
    ]

    result = GEPA._balanced_select(individuals, 1)

    print(f"Test status_priority_ordering: selected {result[0]['version']}")
    # active should be selected even though it has lower win_rate
    assert result[0]["status"] == "active", f"Expected active, got {result[0]['status']}"
    assert result[0]["version"] == "v_active"
    print("  ✅ PASSED")


if __name__ == "__main__":
    print("=" * 60)
    print("GEPA _balanced_select() Unit Tests")
    print("=" * 60)

    test_basic_5_roles()
    test_roles_exceed_size()
    test_all_candidate()
    test_guard_flood()
    test_empty_input()
    test_single_role()
    test_status_priority_ordering()

    print()
    print("=" * 60)
    print("ALL TESTS PASSED ✅")
    print("=" * 60)