from typing import Dict, Callable, Optional, List
from core.enums import Role
from core.game_state import AgentGameState as GameState


# 阶段常量（字符串）
INIT = "init"
START_GAME = "start_game"

# === 夜晚阶段 ===
NIGHT_BEGIN = "night_begin"
GUARD_ACTION_BEGIN = "guard_action_begin"
GUARD_ACTION = "guard_action"
WOLF_GESTURE_BEGIN = "wolf_gesture_begin"
WOLF_GESTURE = "wolf_gesture"
WOLF_KILL_BEGIN = "wolf_kill_begin"
WOLF_KILL = "wolf_kill"
SEER_CHECK_BEGIN = "seer_check_begin"
SEER_CHECK = "seer_check"
WITCH_ACTION_BEGIN = "witch_action_begin"
WITCH_ACTION = "witch_action"
DEATH_SETTLEMENT = "death_settlement"
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

# === 白天阶段 ===
CHECK_GAME_END = "check_game_end"
DISCUSS_BEGIN = "discuss_begin"
SHERIFF_CHOOSE = "sheriff_choose"
DISCUSSION = "discussion"
VOTE = "vote"

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
    WOLF_GESTURE_BEGIN, WOLF_GESTURE, 
    WOLF_KILL_BEGIN, WOLF_KILL,
    SEER_CHECK_BEGIN, SEER_CHECK, 
    WITCH_ACTION_BEGIN, WITCH_ACTION, 
    DEATH_SETTLEMENT,
    SHOOT_REMINDER, DAWN_REPORT,
    ELECTION_BEGIN, SHERIFF_ELECTION_SIGNUP, SHERIFF_ELECTION_SPEECH,
    SHERIFF_ELECTION_VOTE, SHERIFF_ELECTION_RESULT, SHERIFF_PK_SPEECH, SHERIFF_PK_VOTE,
    CHECK_GAME_END, DISCUSS_BEGIN,
    SHERIFF_CHOOSE, DISCUSSION, VOTE,
    SHOOT_SKILL,
    SHERIFF_TRANSFER, LAST_WORDS,
    GAME_OVER,
]


# 阶段跳过条件：静态条件检查（角色死亡等）
PHASE_SKIP_CONDITIONS: Dict[str, Callable[[GameState], bool]] = {
    GUARD_ACTION: lambda s: not s.is_role_alive(Role.GUARD),
    SEER_CHECK: lambda s: not s.is_role_alive(Role.SEER),
    WITCH_ACTION: lambda s: not s.is_role_alive(Role.WITCH),
    SHERIFF_CHOOSE: lambda s: s.sheriff is None,
    WOLF_GESTURE: lambda s: len(s.alive_wolves) <= 1,
}


def _resolve_skip(state: GameState, phase: str) -> str:
    """递归解析阶段跳过，返回第一个不跳过的阶段"""
    while phase in PHASE_SKIP_CONDITIONS and PHASE_SKIP_CONDITIONS[phase](state):
        transition = PHASE_TRANSITIONS.get(phase)
        if not transition:
            break
        phase = transition(state)
    return phase


# 阶段转移函数：与 Mermaid 状态图完全对应
PHASE_TRANSITIONS: Dict[str, Callable[[GameState], str]] = {
    # [*] --> START_GAME
    INIT: lambda s: START_GAME,

    # START_GAME --> NIGHT_BEGIN
    START_GAME: lambda s: NIGHT_BEGIN,

    # NIGHT_BEGIN --> GUARD_ACTION_BEGIN
    NIGHT_BEGIN: lambda s: GUARD_ACTION_BEGIN,

    GUARD_ACTION_BEGIN: lambda s: GUARD_ACTION,

    # GUARD_ACTION --> WOLF_GESTURE_BEGIN
    GUARD_ACTION: lambda s: WOLF_GESTURE_BEGIN,

    WOLF_GESTURE_BEGIN: lambda s: WOLF_GESTURE,

    # WOLF_GESTURE --> WOLF_KILL_BEGIN
    WOLF_GESTURE: lambda s: WOLF_KILL_BEGIN,

    WOLF_KILL_BEGIN: lambda s: WOLF_KILL,

    # WOLF_KILL --> SEER_CHECK_BEGIN
    WOLF_KILL: lambda s: SEER_CHECK_BEGIN,

    SEER_CHECK_BEGIN: lambda s: SEER_CHECK,

    # SEER_CHECK --> WITCH_ACTION_BEGIN
    SEER_CHECK: lambda s: WITCH_ACTION_BEGIN,

    WITCH_ACTION_BEGIN: lambda s: WITCH_ACTION,

    # WITCH_ACTION --> (ELECTION_BEGIN if Day 1) | DEATH_SETTLEMENT (重点：竞选发生在结算前)
    WITCH_ACTION: lambda s: (
        ELECTION_BEGIN if s.day == 1 and s.sheriff is None
        else DEATH_SETTLEMENT
    ),

    # ELECTION_BEGIN --> SHERIFF_ELECTION_SIGNUP
    ELECTION_BEGIN: lambda s: SHERIFF_ELECTION_SIGNUP,

    # SHERIFF_ELECTION_SIGNUP --> candidateCount
    SHERIFF_ELECTION_SIGNUP: lambda s: (
        SHERIFF_ELECTION_RESULT if len(s.sheriff_candidates) <= 1
        else SHERIFF_ELECTION_SPEECH
    ),

    # SHERIFF_ELECTION_SPEECH --> SHERIFF_ELECTION_VOTE
    SHERIFF_ELECTION_SPEECH: lambda s: SHERIFF_ELECTION_VOTE,

    # SHERIFF_ELECTION_VOTE --> voteResult
    SHERIFF_ELECTION_VOTE: lambda s: (
        _get_sheriff_voting_next(s)
    ),

    # SHERIFF_PK_SPEECH --> SHERIFF_PK_VOTE
    SHERIFF_PK_SPEECH: lambda s: SHERIFF_PK_VOTE,

    # SHERIFF_PK_VOTE --> SHERIFF_ELECTION_RESULT
    SHERIFF_PK_VOTE: lambda s: SHERIFF_ELECTION_RESULT,

    # SHERIFF_ELECTION_RESULT --> DEATH_SETTLEMENT
    SHERIFF_ELECTION_RESULT: lambda s: DEATH_SETTLEMENT,

    # DEATH_SETTLEMENT --> SHOOT_REMINDER
    DEATH_SETTLEMENT: lambda s: SHOOT_REMINDER,

    # SHOOT_REMINDER --> DAWN_REPORT
    SHOOT_REMINDER: lambda s: DAWN_REPORT,

    # DAWN_REPORT --> SHOOT_SKILL
    DAWN_REPORT: lambda s: SHOOT_SKILL,

    # DISCUSS_BEGIN --> SHERIFF_CHOOSE
    DISCUSS_BEGIN: lambda s: SHERIFF_CHOOSE,

    # SHERIFF_CHOOSE --> DISCUSSION
    SHERIFF_CHOOSE: lambda s: DISCUSSION,

    # DISCUSSION --> VOTE
    DISCUSSION: lambda s: VOTE,

    # VOTE --> SHOOT_SKILL
    VOTE: lambda s: SHOOT_SKILL,

    # SHOOT_SKILL loops to itself if there are more pending shoots, else SHERIFF_TRANSFER
    SHOOT_SKILL: lambda s: (
        _get_shoot_skill_next(s)
    ),

    # SHERIFF_TRANSFER --> LAST_WORDS
    SHERIFF_TRANSFER: lambda s: LAST_WORDS,

    # LAST_WORDS --> router
    LAST_WORDS: lambda s: (
        GAME_OVER if s.check_game_end()
        else (
            DISCUSS_BEGIN if getattr(s, '_transition_context', {}).get('last_words_source') == 'night'
            else NIGHT_BEGIN
        )
    ),

    # GAME_OVER stays
    GAME_OVER: lambda s: GAME_OVER,
}


def _get_sheriff_voting_next(state: GameState) -> str:
    """SHERIFF_ELECTION_VOTE 后的条件转移"""
    ctx = getattr(state, '_transition_context', {})
    vote_result = ctx.get('sheriff_vote_result', 'elected')
    if vote_result == 'tie':
        return SHERIFF_PK_SPEECH
    return SHERIFF_ELECTION_RESULT


def _get_shoot_skill_next(state: GameState) -> str:
    """SHOOT_SKILL 后的条件转移"""
    ctx = getattr(state, '_transition_context', {})
    pending = ctx.get('pending_shooters', [])
    if pending:
        return SHOOT_SKILL
    ctx.pop('pending_shooters', None)
    return SHERIFF_TRANSFER


class StateMachine:
    """游戏状态机 — 与 Mermaid 状态图完全对应"""

    def __init__(self, state: GameState):
        self._state = state
        self._current_phase = INIT

    @property
    def current_phase(self) -> str:
        return self._current_phase

    def get_canonical_flow(self) -> List[Dict]:
        """生成标准的游戏流参考时间轴"""
        flow = []
        # 夜晚
        flow.append({"phase": "night_begin", "name": "Night Falls"})
        flow.append({"phase": "guard_action", "name": "Guard Protection"})
        flow.append({"phase": "wolf_kill", "name": "Wolf Kill"})
        flow.append({"phase": "seer_check", "name": "Seer Check"})
        flow.append({"phase": "witch_action", "name": "Witch Action"})
        
        # 警长竞选 (仅 Day 1)
        flow.append({"phase": "election", "name": "Sheriff Election", "day_limit": 1})
        
        flow.append({"phase": "dawn_report", "name": "Night Result"})
        
        # 白天
        flow.append({"phase": "discussion", "name": "Daytime Discussion"})
        flow.append({"phase": "vote", "name": "Daytime Exile Vote"})
        flow.append({"phase": "last_words", "name": "Last Words"})
        return flow

    def check_skip(self, phase: str) -> bool:
        """外部查询阶段是否应跳过（例如神职死亡）"""
        return self.should_skip(phase)

    def next_phase(self) -> str:
        """计算并转移到下一阶段"""
        if self._current_phase == INIT:
            self._current_phase = START_GAME
            self._state.phase = self._current_phase
            return self._current_phase

        # 获取转移函数
        transition = PHASE_TRANSITIONS.get(self._current_phase)
        if not transition:
            raise ValueError(
                f"No transition defined for phase: {self._current_phase}"
            )

        next_p = transition(self._state)

        # 检查是否需要跳过（角色死亡等静态条件）
        next_p = _resolve_skip(self._state, next_p)

        self._current_phase = next_p
        self._state.phase = next_p
        return next_p

    def set_phase(self, phase: str) -> None:
        """直接设置当前阶段（用于 engine 内部条件分支跳转）"""
        self._current_phase = phase
        self._state.phase = phase

    def should_skip(self, phase: str) -> bool:
        """检查阶段是否应该被跳过"""
        if phase not in PHASE_SKIP_CONDITIONS:
            return False
        return PHASE_SKIP_CONDITIONS[phase](self._state)
