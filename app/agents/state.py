from typing import TypedDict, List, Dict, Optional, Any


class AgentState(TypedDict):
    # Identity
    room_id: str
    me_id: str
    my_role: str
    # Full agent identifier (e.g. "1_seer") and the session this instance
    # belongs to. session_id is the SessionStore key (per-instance isolation);
    # empty for HTTP callers, which route by agent_id.
    agent_id: str
    session_id: str

    # Game state (all JSON-serializable for persistence)
    phase: str
    day: int
    round: int
    sheriff: Optional[str]
    players: Dict[str, Dict[str, Any]]
    events: List[Dict[str, Any]]

    # Agent cognition
    last_thought: str
    next_action: Optional[Dict[str, Any]]
    personality_prompt: str

    # Current request from game server
    request: Optional[Dict[str, Any]]

    # Memory & evolution
    working_memory: Optional[Dict[str, Any]]
    strategies_used: List[str]
    selected_strategies: List[str]
    versions_used: Dict[str, str]
    in_game_flags: List[Dict[str, Any]]


def make_initial_state(agent_id: str) -> AgentState:
    parts = agent_id.split("_")
    player_id = parts[0]
    role_val = parts[1]

    players = {}
    for i in range(1, 13):
        pid = str(i)
        players[pid] = {
            "id": pid,
            "name": f"Player {pid}",
            "role": role_val if pid == player_id else None,
            "is_alive": True,
        }

    return {
        "room_id": "unknown",
        "me_id": player_id,
        "my_role": role_val,
        "agent_id": agent_id,
        "session_id": "",
        "phase": "init",
        "day": 1,
        "round": 1,
        "sheriff": None,
        "players": players,
        "events": [],
        "last_thought": "",
        "next_action": None,
        "personality_prompt": "",
        "request": None,
        "working_memory": {
            "game_id": "unknown",
            "my_role": role_val,
            "my_seat": player_id,
            "day": 1,
            "known_info": [],
            "speeches": {},
            "actions": [],
            "my_speeches": {},
            "contradictions": [],
            "flags": [],
            "suspicion": {"高": [], "中": [], "低": []},
        },
        "strategies_used": [],
        "selected_strategies": [],
        "versions_used": {},
        "in_game_flags": [],
    }
