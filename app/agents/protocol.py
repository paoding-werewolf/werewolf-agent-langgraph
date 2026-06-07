"""Normalize wire statuses from the game service into local phase names."""

from __future__ import annotations

from typing import Any, Iterable


EVENT_STATUS_MAP = {
    "start": "start_game",
    "night": "night_begin",
    "night_info": "dawn_report",
    "death_notice": "death_settlement",
    "last_words": "last_words",
    "wolf_chat": "wolf_chat",
    "discuss": "discussion",
    "sheriff_election": "sheriff_election_signup",
    "sheriff_speech": "sheriff_election_speech",
    "sheriff_vote": "sheriff_election_vote",
    "sheriff_pk_vote_result": "sheriff_pk_vote_result",
    "sheriff_speech_order": "sheriff_choose",
    "sheriff_pk": "sheriff_pk_speech",
    "sheriff": "sheriff_election_result",
    "sheriff_transfer": "sheriff_transfer",
    "hunter": "shoot_begin",
    "shoot_reminder": "shoot_reminder",
    "result": "game_over",
}

ACTION_STATUS_MAP = {
    **EVENT_STATUS_MAP,
    "hunter": "shoot_skill",
    "sheriff": "sheriff_transfer",
}

SKILL_MESSAGE_MAP = (
    ("guard_action", ("守护", "守卫", "protect")),
    ("seer_check", ("查验", "预言家", "check")),
    ("wolf_kill", ("击杀", "刀", "狼人", "kill")),
    ("witch_action", ("解药", "毒药", "女巫", "heal", "poison")),
)

SKILL_CONTEXT_MAP = {
    "guard_action": "guard_action",
    "seer_check": "seer_check",
    "wolf_kill": "wolf_kill",
    "witch_action": "witch_action",
    # 兼容此前从服务端复制过来的 begin-phase。
    "guard_action_begin": "guard_action",
    "seer_check_begin": "seer_check",
    "wolf_kill_begin": "wolf_kill",
    "witch_action_begin": "witch_action",
}


def normalize_status(status: Any, *, mode: str = "event") -> str:
    value = str(status or "")
    mapping = ACTION_STATUS_MAP if mode == "action" else EVENT_STATUS_MAP
    return mapping.get(value, value)


def normalize_action_status(status: Any, *, message: Any = "", previous_phase: Any = "") -> str:
    value = str(status or "")
    text = str(message or "").lower()

    if value in {"last_words", "last_words_action"}:
        return "last_words"

    if value == "discuss" and ("遗言" in text or "last word" in text):
        return "last_words"

    if value != "skill":
        return normalize_status(value, mode="action")

    for phase, tokens in SKILL_MESSAGE_MAP:
        if any(token in text for token in tokens):
            return phase

    previous = normalize_status(previous_phase, mode="event")
    return SKILL_CONTEXT_MAP.get(previous, value)


def normalize_event_status(status: Any, traces: Iterable[dict] | None = None, *, message: Any = "") -> str:
    value = str(status or "")
    actions = {trace.get("action") for trace in (traces or [])}
    text = str(message or "").lower()
    if value == "hunter":
        if actions & {"shoot_skill"} or "开枪" in text or "放弃" in text:
            return "shoot_skill"
        return "shoot_begin"

    if value == "skill_result":
        if "你可以开枪" in text or "无法开枪" in text:
            return "shoot_reminder"
        if actions & {"guard_protect"}:
            return "guard_action"
        if actions & {"wolf_kill"}:
            return "wolf_kill"
        if actions & {"seer_wolf", "seer_good"}:
            return "seer_check"
        if actions & {"witch_heal", "witch_poison"}:
            return "witch_action"
        if actions & {"shoot_skill"}:
            return "shoot_skill"

        for phase, tokens in SKILL_MESSAGE_MAP:
            if any(token in text for token in tokens):
                return phase

    if value == "sheriff":
        if actions & {"sheriff_transfer", "sheriff_destroy"}:
            return "sheriff_transfer"
    return normalize_status(value, mode="event")
