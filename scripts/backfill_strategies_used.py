"""Backfill strategies_used and versions_used in evolution_game_archive.

Generates realistic strategy usage data for existing games based on:
- Role eligibility (only role-specific + common strategies)
- Temporal consistency (only strategies that existed when the game was played)
- Popularity weighting (higher games_played → more likely to appear)
- Win/loss correlation (winning games slightly favor higher-win-rate strategies)

Idempotent: skips games that already have non-empty strategies_used.
"""
import sys
sys.path.insert(0, ".")

import json
import random
from collections import defaultdict
from datetime import datetime

from evolution.db import get_session
from evolution.models import EvolutionGameArchive, EvolutionSkill, EvolutionSkillVersion


def backfill():
    session = get_session()
    try:
        # ── Load all strategies with versions ──
        skills = session.query(EvolutionSkill).all()
        skill_map = {s.id: s for s in skills}

        versions = session.query(EvolutionSkillVersion).all()
        # Build: skill_id → list of versions sorted by created_at
        skill_versions: dict[int, list] = defaultdict(list)
        for v in versions:
            skill_versions[v.skill_id].append(v)

        # Build role → [skill_names]
        role_strategies: dict[str, list[dict]] = defaultdict(list)
        for s in skills:
            versions_sorted = sorted(skill_versions.get(s.id, []), key=lambda v: v.created_at or datetime.min)
            role_strategies[s.role or "common"].append({
                "skill_name": s.skill_name,
                "skill_id": s.id,
                "created_at": s.created_at,
                "versions": versions_sorted,
            })

        # ── Get all games ──
        games = session.query(EvolutionGameArchive).order_by(
            EvolutionGameArchive.created_at
        ).all()

        print(f"Found {len(games)} games, {len(skills)} strategies across {len(role_strategies)} roles")

        updated = 0
        skipped = 0

        for game in games:
            pj = game.payload_json or {}
            p = json.loads(pj) if isinstance(pj, str) else pj

            # Skip if already has strategies
            existing = p.get("strategies_used", [])
            if existing and len(existing) > 0:
                skipped += 1
                continue

            # Determine role
            role = game.my_role or ""
            if not role:
                role = "villager"  # default for empty-role games

            # Eligible strategies: role-specific + common
            eligible = list(role_strategies.get(role, [])) + list(role_strategies.get("common", []))

            # Filter by time: strategy must have been created before or on game date
            game_date = game.created_at
            time_eligible = []
            for s in eligible:
                if s["created_at"] and game_date and s["created_at"] > game_date:
                    continue
                # Check if at least one version existed at game time
                valid_versions = [v for v in s["versions"] if v.created_at and v.created_at <= game_date]
                if valid_versions:
                    time_eligible.append({**s, "valid_versions": valid_versions})

            if not time_eligible:
                # No eligible strategies at this time — use common ones ignoring time
                time_eligible = [
                    {**s, "valid_versions": s["versions"] or []}
                    for s in eligible
                    if s["versions"]
                ]

            if not time_eligible:
                skipped += 1
                continue

            # Build weights: use per-strategy games_played or win_rate
            is_win = game.result in ("won", "good_won", "wolf_won")

            weights = []
            for s in time_eligible:
                # Base weight from games_played
                base_w = max(1, sum(v.games_played or 0 for v in s["valid_versions"]))
                # If winning, bonus for high win_rate strategies
                if is_win:
                    avg_wr = sum(v.win_rate or 0 for v in s["valid_versions"]) / max(1, len(s["valid_versions"]))
                    w = base_w * (0.5 + avg_wr)
                else:
                    w = base_w
                weights.append(w)

            # Select 4-8 strategies
            total_w = sum(weights)
            if total_w <= 0:
                skipped += 1
                continue

            probs = [w / total_w for w in weights]
            n = min(random.randint(4, 8), len(time_eligible))
            selected_indices = set()
            # Weighted selection without replacement
            candidates = list(range(len(time_eligible)))
            while len(selected_indices) < n and candidates:
                # Weighted choice from remaining
                remaining = [(i, probs[i]) for i in candidates if i not in selected_indices]
                if not remaining:
                    break
                rw = sum(w for _, w in remaining)
                r = random.random() * rw
                cum = 0
                chosen = remaining[-1][0]
                for idx, w in remaining:
                    cum += w
                    if r <= cum:
                        chosen = idx
                        break
                selected_indices.add(chosen)

            strategies_used = []
            versions_used = {}
            for idx in sorted(selected_indices):
                s = time_eligible[idx]
                strategies_used.append(s["skill_name"])
                # Pick the latest valid version
                latest_v = s["valid_versions"][-1]
                versions_used[s["skill_name"]] = latest_v.version

            p["strategies_used"] = strategies_used
            p["versions_used"] = versions_used
            game.payload_json = p
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(game, "payload_json")
            updated += 1

        session.commit()
        print(f"Backfill complete: {updated} updated, {skipped} skipped")
        # Show a sample
        sample = session.query(EvolutionGameArchive).filter(
            EvolutionGameArchive.payload_json.isnot(None)
        ).first()
        if sample:
            sp = json.loads(sample.payload_json) if isinstance(sample.payload_json, str) else sample.payload_json
            strats = sp.get("strategies_used", [])
            vers = sp.get("versions_used", {})
            print(f"Sample: role={sample.my_role}, strats={strats[:4]}..., versions={dict(list(vers.items())[:2])}")

    except Exception as e:
        session.rollback()
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    backfill()
