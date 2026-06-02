from enum import Enum
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field


class Role(str, Enum):
    """角色枚举"""
    WOLF = "wolf"
    WOLF_KING = "wolf_king"
    SEER = "seer"
    WITCH = "witch"
    GUARD = "guard"
    HUNTER = "hunter"
    VILLAGER = "villager"

    @property
    def is_wolf_team(self) -> bool:
        return self in (Role.WOLF, Role.WOLF_KING)

    @property
    def is_god(self) -> bool:
        return self in (Role.SEER, Role.WITCH, Role.GUARD, Role.HUNTER)


class GamePhase:
    """游戏阶段常量（字符串，非枚举）"""
    # === 初始化 ===
    INIT = "init"
    START_GAME = "start_game"

    # === 夜晚阶段 ===
    NIGHT_BEGIN = "night_begin"
    GUARD_ACTION_BEGIN = "guard_action_begin"
    GUARD_ACTION = "guard_action"
    WOLF_CHAT_BEGIN = "wolf_chat_begin"
    WOLF_CHAT = "wolf_chat"
    WOLF_GESTURE_BEGIN = "wolf_gesture_begin"
    WOLF_GESTURE = "wolf_gesture"
    WOLF_KILL_BEGIN = "wolf_kill_begin"
    WOLF_KILL = "wolf_kill"
    WOLF_KILL_RESULT = "wolf_kill_result"
    SEER_CHECK_BEGIN = "seer_check_begin"
    SEER_CHECK = "seer_check"
    WITCH_ACTION_BEGIN = "witch_action_begin"
    WITCH_ACTION = "witch_action"
    DEATH_SETTLEMENT = "death_settlement"
    HUNTER_REMINDER_BEGIN = "hunter_reminder_begin"
    WOLF_KING_REMINDER_BEGIN = "wolf_king_reminder_begin"
    SHOOT_BEGIN = "shoot_begin"
    SHOOT_REMINDER = "shoot_reminder"
    DAWN_REPORT = "dawn_report"

    # === 警长选举 (仅Day 1) ===
    ELECTION_BEGIN = "election_begin"
    SHERIFF_ELECTION_SIGNUP = "sheriff_election_signup"
    SHERIFF_ELECTION_SPEECH = "sheriff_election_speech"
    SHERIFF_ELECTION_VOTE = "sheriff_election_vote"
    SHERIFF_ELECTION_RESULT = "sheriff_election_result"
    SHERIFF_PK_SPEECH = "sheriff_pk_speech"
    SHERIFF_PK_VOTE = "sheriff_pk_vote"
    SHERIFF_PK_VOTE_RESULT = "sheriff_pk_vote_result"

    # === 白天阶段 ===
    CHECK_GAME_END = "check_game_end"
    DISCUSS_BEGIN = "discuss_begin"
    SHERIFF_CHOOSE = "sheriff_choose"
    DISCUSSION = "discussion"
    VOTE = "vote"
    VOTE_RESULT = "vote_result"

    # === 出局处理 ===
    SHOOT_SKILL = "shoot_skill"
    SHERIFF_TRANSFER = "sheriff_transfer"
    LAST_WORDS = "last_words"

    # === 终局 ===
    GAME_OVER = "game_over"

    ALL_PHASES = [
        INIT, START_GAME,
        NIGHT_BEGIN,
        GUARD_ACTION_BEGIN, GUARD_ACTION,
        WOLF_CHAT_BEGIN, WOLF_CHAT,
        WOLF_GESTURE_BEGIN, WOLF_GESTURE,
        WOLF_KILL_BEGIN, WOLF_KILL, WOLF_KILL_RESULT,
        SEER_CHECK_BEGIN, SEER_CHECK,
        WITCH_ACTION_BEGIN, WITCH_ACTION,
        DEATH_SETTLEMENT, HUNTER_REMINDER_BEGIN, WOLF_KING_REMINDER_BEGIN,
        SHOOT_BEGIN, SHOOT_REMINDER, DAWN_REPORT,
        ELECTION_BEGIN, SHERIFF_ELECTION_SIGNUP, SHERIFF_ELECTION_SPEECH,
        SHERIFF_ELECTION_VOTE, SHERIFF_ELECTION_RESULT, SHERIFF_PK_SPEECH, SHERIFF_PK_VOTE, SHERIFF_PK_VOTE_RESULT,
        CHECK_GAME_END, DISCUSS_BEGIN,
        SHERIFF_CHOOSE, DISCUSSION, VOTE, VOTE_RESULT,
        SHOOT_SKILL,
        SHERIFF_TRANSFER, LAST_WORDS,
        GAME_OVER,
    ]


# 阶段配置：自包含可见性和适用角色
# is_global: 是否为全局广播（如“天黑了”）
# applicable_roles: 哪些角色在该阶段有特殊的“睁眼”视角或行动权
PHASE_CONFIG = {
    # 夜晚
    GamePhase.NIGHT_BEGIN: {"is_global": True, "applicable_roles": [], "group": "night_begin"},
    GamePhase.GUARD_ACTION_BEGIN: {"is_global": True, "applicable_roles": [], "group": "guard_action"},
    GamePhase.GUARD_ACTION: {"is_global": False, "applicable_roles": [Role.GUARD], "group": "guard_action"},
    GamePhase.WOLF_CHAT_BEGIN: {"is_global": True, "applicable_roles": [], "group": "wolf_kill"},
    GamePhase.WOLF_CHAT: {"is_global": False, "applicable_roles": [Role.WOLF, Role.WOLF_KING], "group": "wolf_kill"},
    GamePhase.WOLF_GESTURE_BEGIN: {"is_global": True, "applicable_roles": [], "group": "wolf_kill"},
    GamePhase.WOLF_GESTURE: {"is_global": False, "applicable_roles": [Role.WOLF, Role.WOLF_KING], "group": "wolf_kill"},
    GamePhase.WOLF_KILL_BEGIN: {"is_global": True, "applicable_roles": [], "group": "wolf_kill"},
    GamePhase.WOLF_KILL: {"is_global": False, "applicable_roles": [Role.WOLF, Role.WOLF_KING], "group": "wolf_kill"},
    GamePhase.WOLF_KILL_RESULT: {"is_global": False, "applicable_roles": [Role.WOLF, Role.WOLF_KING], "group": "wolf_kill"},
    GamePhase.SEER_CHECK_BEGIN: {"is_global": True, "applicable_roles": [], "group": "seer_check"},
    GamePhase.SEER_CHECK: {"is_global": False, "applicable_roles": [Role.SEER], "group": "seer_check"},
    GamePhase.WITCH_ACTION_BEGIN: {"is_global": True, "applicable_roles": [], "group": "witch_action"},
    GamePhase.WITCH_ACTION: {"is_global": False, "applicable_roles": [Role.WITCH], "group": "witch_action"},
    GamePhase.DEATH_SETTLEMENT: {"is_global": False, "applicable_roles": [], "group": "dawn_report"},
    GamePhase.HUNTER_REMINDER_BEGIN: {"is_global": True, "applicable_roles": [], "group": "dawn_report"},
    GamePhase.WOLF_KING_REMINDER_BEGIN: {"is_global": True, "applicable_roles": [], "group": "dawn_report"},
    GamePhase.SHOOT_BEGIN: {"is_global": True, "applicable_roles": [], "group": "dawn_report"},
    GamePhase.SHOOT_REMINDER: {"is_global": False, "applicable_roles": [Role.HUNTER, Role.WOLF_KING], "group": "dawn_report"},
    GamePhase.DAWN_REPORT: {"is_global": True, "applicable_roles": [], "group": "dawn_report"},

    # 竞选
    GamePhase.ELECTION_BEGIN: {"is_global": True, "applicable_roles": [], "group": "election"},
    GamePhase.SHERIFF_ELECTION_SIGNUP: {"is_global": True, "applicable_roles": [], "group": "election"},
    GamePhase.SHERIFF_ELECTION_SPEECH: {"is_global": True, "applicable_roles": [], "group": "election"},
    GamePhase.SHERIFF_ELECTION_VOTE: {"is_global": True, "applicable_roles": [], "group": "election"},
    GamePhase.SHERIFF_ELECTION_RESULT: {"is_global": True, "applicable_roles": [], "group": "election"},
    GamePhase.SHERIFF_PK_SPEECH: {"is_global": True, "applicable_roles": [], "group": "election"},
    GamePhase.SHERIFF_PK_VOTE: {"is_global": True, "applicable_roles": [], "group": "election"},
    GamePhase.SHERIFF_PK_VOTE_RESULT: {"is_global": True, "applicable_roles": [], "group": "election"},

    # 白天
    GamePhase.DISCUSS_BEGIN: {"is_global": True, "applicable_roles": [], "group": "discussion"},
    GamePhase.SHERIFF_CHOOSE: {"is_global": True, "applicable_roles": [], "group": "discussion"},
    GamePhase.DISCUSSION: {"is_global": True, "applicable_roles": [], "group": "discussion"},
    GamePhase.VOTE: {"is_global": True, "applicable_roles": [], "group": "vote"},
    GamePhase.VOTE_RESULT: {"is_global": True, "applicable_roles": [], "group": "vote"},
    
    # 出局
    GamePhase.SHOOT_SKILL: {"is_global": True, "applicable_roles": [Role.HUNTER, Role.WOLF_KING], "group": "shoot_skill"},
    GamePhase.SHERIFF_TRANSFER: {"is_global": True, "applicable_roles": [], "group": "last_words"},
    GamePhase.LAST_WORDS: {"is_global": True, "applicable_roles": [], "group": "last_words"},
    
    GamePhase.GAME_OVER: {"is_global": True, "applicable_roles": [], "group": "game_over"},
}



class WolfGesture(str, Enum):
    """狼人夜间手势"""
    POINT = "point"           # 指认目标
    LOWKEY = "lowkey"         # 保持低调
    SHIFT = "shift"           # 转移焦点
    CHANGE = "change"         # 改变策略
    AGREE = "agree"           # 同意/确认
    PASS = "pass"             # 弃权


class PlayerType(str, Enum):
    """玩家类型"""
    BUILTIN_AI = "builtin_ai"
    HUMAN = "human"
    HTTP_AGENT = "http_agent"
    RANDOM = "random"


class TraceAction(str, Enum):
    """轨迹语义动作"""
    # 发言/动作
    SPEAK = "speak"
    SIGNUP_SHERIFF = "signup_sheriff"
    GIVE_UP_SHERIFF = "give_up_sheriff"
    
    # 投票
    VOTE_SHERIFF = "vote_sheriff"
    VOTE_ELIMINATE = "vote_eliminate"
    
    # 技能
    GUARD_PROTECT = "guard_protect"
    WOLF_GESTURE = "wolf_gesture"
    WOLF_KILL = "wolf_kill"
    SEER_WOLF = "seer_wolf"
    SEER_GOOD = "seer_good"
    WITCH_HEAL = "witch_heal"
    WITCH_POISON = "witch_poison"
    SHOOT_SKILL = "shoot_skill"
    
    # 系统/其他
    SHERIFF_TRANSFER = "sheriff_transfer"
    SHERIFF_DESTROY = "sheriff_destroy"


@dataclass
class PlayerState:
    """玩家状态"""
    id: str
    name: str
    role: Role
    is_alive: bool = True
    is_sheriff: bool = False
    is_guarded: bool = False
    is_healed: bool = False
    is_poisoned: bool = False
    can_shoot: bool = False
    last_guarded: Optional[str] = None  # 上一晚守护目标


@dataclass
class DeathResult:
    """夜间死亡结算结果"""
    wolf_killed: Optional[str] = None
    poison_killed: Optional[str] = None
    paradox_killed: Optional[str] = None


@dataclass
class GameEvent:
    """游戏事件 — 采用 ACT/PERCEIVE 二元协议"""
    type: str                  # 业务类型 (wolf_kill, vote...)
    round: int                 # 轮次
    status: str = "notify"     # act (请求) | perceive (感知/结果)
    content: Optional[str] = None # 主持人视角的宣布文本
    # 行为轨迹 [{"from": "1", "to": "2", "action": "vote_eliminate"}, ...]
    traces: List[Dict[str, Any]] = field(default_factory=list)
    visible_to: List[str] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)

    def is_visible_to(self, player_id: str) -> bool:
        if not self.visible_to:
            return True
        return player_id in self.visible_to


@dataclass
class ActRequest:
    """行动请求"""
    action_type: str
    message: str
    round: int
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentAction:
    """Agent行动结果"""
    player_id: str
    action_type: str
    content: Optional[str] = None
    target_player: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)
