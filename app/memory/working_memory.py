"""memory/working_memory.py — 单局工作记忆

生命周期：单局（从发牌到游戏结束）
加载方式：始终在 prompt 中
Token 预算：~2,000 tokens（滚动压缩）
"""
import json
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field


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

        elif status in ("discussion", "last_words"):
            self.speeches.setdefault(day_key, {})
            self.speeches[day_key]["summary"] = content

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
            lines.append("\n### Speech Summaries")
            for day_key, day_speeches in sorted(self.speeches.items()):
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
