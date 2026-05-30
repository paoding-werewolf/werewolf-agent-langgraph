from typing import TypedDict, List, Dict, Optional, Any, Annotated
from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage


class AgentState(TypedDict):
    # Identity
    room_id: str
    me_id: str
    my_role: str
    # Full agent identifier (e.g. "1_seer") and the WS session this instance
    # belongs to. session_id == checkpointer thread_id; empty for HTTP callers
    # that fall back to routing by agent_id.
    agent_id: str
    session_id: str

    # Game state (all JSON-serializable for checkpointing)
    phase: str
    day: int
    round: int
    sheriff: Optional[str]
    players: Dict[str, Dict[str, Any]]
    events: List[Dict[str, Any]]

    # Agent cognition
    memory: List[Dict[str, Any]]
    last_thought: str
    next_action: Optional[Dict[str, Any]]

    # Current request from game server
    request: Optional[Dict[str, Any]]

    # Message history (managed by add_messages reducer)
    messages: Annotated[List[BaseMessage], add_messages]


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
            "is_sheriff": False,
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
        "memory": [],
        "last_thought": "",
        "next_action": None,
        "request": None,
        "messages": [],
    }