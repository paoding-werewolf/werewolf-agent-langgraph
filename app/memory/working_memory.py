"""memory/working_memory.py — 单局工作记忆

生命周期：单局（从发牌到游戏结束）
加载方式：始终在 prompt 中
Token 预算：~2,000 tokens（滚动压缩）
"""
import json
import re
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field


SPEECH_STATUSES = {
    "discussion",
    "sheriff_election_speech",
    "sheriff_pk_speech",
    "last_words",
}

SPEECH_TRACE_ACTIONS = {
    "speak",
    "speech",
    "discussion",
    "last_words",
    "sheriff_speech",
    "sheriff_pk_speech",
}


@dataclass
class WorkingMemory:
    """单局工作记忆，嵌入 AgentState 中传递。"""
    game_id: str = ""
    my_role: str = ""
    my_seat: str = ""
    day: int = 1

    known_info: List[str] = field(default_factory=list)
    speeches: Dict[str, Dict[str, str]] = field(default_factory=dict)
    actions: List[str] = field(default_factory=list)
    my_speeches: Dict[str, str] = field(default_factory=dict)
    contradictions: List[str] = field(default_factory=list)
    flags: List[Dict] = field(default_factory=list)
    suspicion: Dict[str, List[str]] = field(default_factory=lambda: {"高": [], "中": [], "低": []})

    def update_from_event(self, event: Dict):
        """从 perceive 事件更新工作记忆。"""
        status = event.get("status", "")
        content = event.get("content", "")
        round_num = event.get("round", 1)
        traces = event.get("traces", [])

        day_key = f"D{round_num}"

        if "night" in status or status in ("guard_action", "wolf_kill", "seer_check", "witch_action"):
            for t in traces:
                self.actions.append(f"R{round_num}_{status}: {t.get('from','')}->{t.get('to','')}({t.get('action','')})")
            if content:
                self.known_info.append(f"R{round_num}: {content}")

        elif status in SPEECH_STATUSES:
            self._append_speech(day_key, status, content, event)

        elif status in ("vote", "vote_result", "sheriff_election_vote", "sheriff_pk_vote_result"):
            self.actions.append(f"{day_key}_vote: {content}")

        elif status in ("dawn_report", "shoot_begin"):
            self.actions.append(f"{day_key}_death: {content}")

        elif status in ("sheriff_election_result", "sheriff_transfer"):
            self.actions.append(f"sheriff: {content}")

    def add_my_speech(self, day: int, text: str):
        """记录我的完整发言。"""
        self.my_speeches[f"D{day}"] = text

    def add_flag(self, flag: Dict):
        """添加即时标记。"""
        self.flags.append(flag)

    def format_for_prompt(self) -> str:
        """格式化为 prompt 片段（~2000 tokens 预算）。"""
        lines = [
            "## Working Memory (This Game)",
            f"Role: {self.my_role} | Seat: {self.my_seat} | Day: {self.day}",
        ]

        if self.known_info:
            lines.append("\n### Known Info")
            lines.extend(f"- {info}" for info in self.known_info[-10:])

        if self.speeches:
            lines.append("\n### Speech Timeline")
            for day_key, day_speeches in sorted(self.speeches.items(), key=lambda item: self._day_sort_key(item[0])):
                for speaker, content in day_speeches.items():
                    lines.append(f"- {day_key} {speaker}: {content}")

        if self.actions:
            lines.append("\n### Key Actions")
            lines.extend(f"- {a}" for a in self.actions[-15:])

        if self.my_speeches:
            lines.append("\n### My Speeches (Full)")
            for day_key, text in sorted(self.my_speeches.items()):
                lines.append(f"- {day_key}: \"{text}\"")

        if self.contradictions:
            lines.append("\n### Contradictions Detected")
            lines.extend(f"- {c}" for c in self.contradictions)

        if self.suspicion.get("高"):
            lines.append(f"\n### Suspicion: HIGH={self.suspicion['高']}, MED={self.suspicion.get('中', [])}, LOW={self.suspicion.get('低', [])}")

        return "\n".join(lines)

    def compress_old_entries(self, keep_recent: int = 5):
        """压缩旧条目，控制 token 预算。"""
        if len(self.actions) > keep_recent * 2:
            old = self.actions[:-keep_recent]
            self.actions = [f"[Earlier: {len(old)} events compressed]"] + self.actions[-keep_recent:]

    def _append_speech(self, day_key: str, status: str, content: str, event: Dict):
        """按事件顺序追加发言，避免同一天多名玩家发言互相覆盖。"""
        if not content:
            return

        day_speeches = self.speeches.setdefault(day_key, {})
        sequence = len(day_speeches) + 1
        speaker = self._extract_speaker(event)
        phase_label = self._speech_phase_label(status)
        speaker_label = self._format_speaker_label(speaker)

        key = f"{phase_label}#{sequence:02d}"
        if speaker_label:
            key = f"{key} {speaker_label}"

        while key in day_speeches:
            sequence += 1
            key = f"{phase_label}#{sequence:02d}"
            if speaker_label:
                key = f"{key} {speaker_label}"

        day_speeches[key] = content

    def _extract_speaker(self, event: Dict) -> Optional[str]:
        """从 traces/extra/content 中提取发言人编号，提取不到则返回 None。"""
        extra = event.get("extra") or {}
        for key in ("speaker", "speaker_id", "player", "player_id", "from"):
            value = extra.get(key) or event.get(key)
            if value:
                return str(value)

        for trace in event.get("traces") or []:
            action = trace.get("action")
            if action in SPEECH_TRACE_ACTIONS and trace.get("from"):
                return str(trace["from"])

        content = str(event.get("content") or "")
        match = re.match(r"\s*(?:玩家)?(\d{1,2})\s*号?\s*[：:,，\s]", content)
        if match:
            return match.group(1)
        return None

    def _speech_phase_label(self, status: str) -> str:
        labels = {
            "discussion": "发言",
            "sheriff_election_speech": "警上发言",
            "sheriff_pk_speech": "警长PK发言",
            "last_words": "遗言",
        }
        return labels.get(status, status)

    def _format_speaker_label(self, speaker: Optional[str]) -> str:
        if not speaker:
            return ""
        if speaker.isdigit():
            return f"{speaker}号"
        return speaker

    def _day_sort_key(self, day_key: str) -> tuple[int, str]:
        match = re.match(r"D(\d+)$", day_key)
        if match:
            return int(match.group(1)), day_key
        return 10**9, day_key

    def to_dict(self) -> Dict:
        return {
            "game_id": self.game_id,
            "my_role": self.my_role,
            "my_seat": self.my_seat,
            "day": self.day,
            "known_info": self.known_info,
            "speeches": self.speeches,
            "actions": self.actions,
            "my_speeches": self.my_speeches,
            "contradictions": self.contradictions,
            "flags": self.flags,
            "suspicion": self.suspicion,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "WorkingMemory":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
