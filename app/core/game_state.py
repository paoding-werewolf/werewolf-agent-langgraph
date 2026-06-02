from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field
from core.enums import Role

@dataclass
class PlayerPerception:
    id: str
    name: str
    role: Optional[Role] = None  # None if unknown
    is_alive: bool = True
    is_sheriff: bool = False
    notes: str = "" # AI's private thoughts about this player

@dataclass
class AgentGameState:
    """Agent 感知到的游戏状态"""
    room_id: str
    me_id: str
    my_role: Role
    round: int = 1
    day: int = 1
    phase: str = ""
    players: Dict[str, PlayerPerception] = field(default_factory=dict)
    
    # 历史记录
    events: List[Dict] = field(default_factory=list)
    
    # 公共信息快照
    sheriff: Optional[str] = None
    sheriff_candidates: List[str] = field(default_factory=list)
    dead_this_round: List[str] = field(default_factory=list)
    winner: Optional[str] = None
    
    # AI 内部认知 (Belief Map)
    # player_id -> { "wolf": 0.2, "god": 0.3, "villager": 0.5 }
    beliefs: Dict[str, Dict[str, float]] = field(default_factory=dict)

    def get_player(self, pid: str) -> Optional[PlayerPerception]:
        return self.players.get(pid)

    @property
    def alive_players(self) -> List[PlayerPerception]:
        return [p for p in self.players.values() if p.is_alive]

    def is_role_alive(self, role: Role) -> bool:
        # Agent 只能基于已确定的信息判断
        return any(p.is_alive and p.role == role for p in self.players.values())

    def alive_wolves(self) -> List[PlayerPerception]:
        # 注意：Agent 视角下的 alive_wolves 可能不完整或不准确
        return [p for p in self.alive_players if p.role and p.role.is_wolf_team]

    def check_game_end(self) -> bool:
        return self.winner is not None
