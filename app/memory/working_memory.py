"""memory/working_memory.py — 单局工作记忆

生命周期：单局（从发牌到游戏结束）
加载方式：始终在 prompt 中
Token 预算：~2,000 tokens（滚动压缩）
"""
import re
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field


SPEECH_STATUSES = {
    "discussion",
    "sheriff_election_speech",
    "sheriff_pk_speech",
    "last_words",
}

SPEECH_PHASE_LABELS = {
    "discussion": "讨论",
    "sheriff_election_speech": "警上发言",
    "sheriff_pk_speech": "警长PK发言",
    "last_words": "遗言",
}

_SKIP_KNOWN_INFO_STATUSES = {
    "wolf_kill", "seer_check", "guard_action", "witch_action", "wolf_chat",
    "vote", "vote_result",
    "sheriff_election_vote", "sheriff_pk_vote_result", "sheriff_election_signup",
    "shoot_skill", "shoot_begin",
    "sheriff_election_result", "sheriff_transfer",
    "night_begin",
}

COMPRESS_PROMPT = """请将以下第{day}天的玩家发言压缩为结构化摘要。

要求：
1. 每个发言玩家一行，格式："X号: [关键信息]"
2. 必须保留：
   - 身份声明（谁跳了什么角色、对跳情况）
   - 查验/技能声明（预言家验了谁结果如何等）
   - 明确站边和怀疑对象
   - 关键逻辑论点（核心攻防、反驳要点）
3. 可以省略：
   - 礼貌性开场白和结尾
   - 重复表述
   - 无具体论点的情绪宣泄

发言内容：
{speeches_text}"""

_ACTION_FORMATS = {
    "vote_eliminate": ("投票", lambda a: f"{a.actor}→{a.target}" if a.target else f"{a.actor}:弃"),
    "vote_sheriff": ("警长票", lambda a: f"{a.actor}→{a.target}" if a.target else f"{a.actor}:弃"),
    "death": ("死亡", lambda a: a.target),
    "shoot_skill": ("开枪", lambda a: f"{a.actor}→{a.target}"),
    "seer_wolf": ("查验", lambda a: f"{a.target}=狼"),
    "seer_good": ("查验", lambda a: f"{a.target}=好人"),
    "guard_protect": ("守护", lambda a: a.target),
    "witch_heal": ("救治", lambda a: a.target),
    "witch_poison": ("毒杀", lambda a: a.target),
    "wolf_kill": ("刀", lambda a: a.target),
    "sheriff_transfer": ("警徽", lambda a: f"{a.actor}→{a.target}"),
    "sheriff_destroy": ("撕徽", lambda a: a.actor),
    "signup_sheriff": ("竞选", lambda a: a.actor),
}

_ACTION_DISPLAY_ORDER = (
    "wolf_kill", "seer_wolf", "seer_good", "guard_protect",
    "witch_heal", "witch_poison", "death", "shoot_skill",
    "vote_eliminate", "vote_sheriff", "signup_sheriff",
    "sheriff_transfer", "sheriff_destroy",
)


@dataclass
class GameAction:
    """结构化游戏行为记录，来自事件 traces。"""
    day: int
    phase: str
    actor: str
    target: str
    action_type: str

    def to_dict(self) -> Dict:
        return {
            "day": self.day, "phase": self.phase,
            "actor": self.actor, "target": self.target,
            "action_type": self.action_type,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "GameAction":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class SpeechRecord:
    """玩家发言记录。compressed=True 时 prompt 注入 day_summaries 中的摘要。"""
    raw: str
    summary: str = ""
    compressed: bool = False
    day: int = 0
    phase: str = ""
    speaker: str = ""

    def to_dict(self) -> Dict:
        return {
            "raw": self.raw, "summary": self.summary,
            "compressed": self.compressed, "day": self.day,
            "phase": self.phase, "speaker": self.speaker,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "SpeechRecord":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class WorkingMemory:
    """单局工作记忆，嵌入 AgentState 中传递。"""
    game_id: str = ""
    my_role: str = ""
    my_seat: str = ""
    day: int = 1

    known_info: List[str] = field(default_factory=list)
    speeches: List[SpeechRecord] = field(default_factory=list)
    game_actions: List[GameAction] = field(default_factory=list)
    my_speeches: Dict[str, str] = field(default_factory=dict)
    contradictions: List[str] = field(default_factory=list)
    flags: List[Dict] = field(default_factory=list)
    suspicion: Dict[str, List[str]] = field(default_factory=lambda: {"高": [], "中": [], "低": []})
    day_summaries: Dict[int, str] = field(default_factory=dict)

    # ── 事件更新 ──

    def update_from_event(self, event: Dict):
        """从 perceive 事件更新工作记忆。"""
        status = event.get("status", "")
        content = event.get("content", "")
        round_num = event.get("round", 1)
        traces = event.get("traces", [])

        for t in traces:
            action = str(t.get("action", ""))
            if action == "speak":
                continue
            self.game_actions.append(GameAction(
                day=round_num, phase=status,
                actor=str(t.get("from", "") or ""),
                target=str(t.get("to", "") or ""),
                action_type=action,
            ))

        if status in SPEECH_STATUSES:
            self._add_speech(content, round_num, status, event)
        elif status == "start_game":
            self.known_info.append(f"R{round_num}: 游戏已开始")
        elif status in ("dawn_report", "death_settlement"):
            if content:
                self.known_info.append(f"R{round_num}: {content}")
        elif status not in _SKIP_KNOWN_INFO_STATUSES and content:
            self.known_info.append(f"R{round_num}: {content}")

    def add_my_speech(self, day: int, text: str):
        """记录我的完整发言。"""
        self.my_speeches[f"D{day}"] = text

    def add_flag(self, flag: Dict):
        """添加即时标记。"""
        self.flags.append(flag)

    # ── 压缩 ──

    def has_uncompressed_old_speeches(self) -> bool:
        """是否有两天前的发言尚未压缩。"""
        cutoff = self.day - 1
        return any(s.day < cutoff and not s.compressed for s in self.speeches)

    def compress_old_speeches(self, llm_caller) -> bool:
        """压缩两天前的发言，使用 LLM 生成摘要。每天合并为一次调用。"""
        cutoff = self.day - 1
        to_compress = [s for s in self.speeches if s.day < cutoff and not s.compressed]
        if not to_compress:
            return False

        by_day: Dict[int, List[SpeechRecord]] = {}
        for s in to_compress:
            by_day.setdefault(s.day, []).append(s)

        for day, day_speeches in by_day.items():
            speeches_text = "\n".join(
                f"{s.speaker}号: {s.raw}" for s in day_speeches
            )
            prompt = COMPRESS_PROMPT.format(day=day, speeches_text=speeches_text)
            try:
                resp = llm_caller.client.chat.completions.create(
                    model=llm_caller.model,
                    messages=[
                        {"role": "system", "content": "你是狼人杀发言摘要专家。输出简洁的结构化摘要。"},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.2,
                )
                summary = resp.choices[0].message.content or ""
                self.day_summaries[day] = summary
                for s in day_speeches:
                    s.compressed = True
            except Exception:
                pass

        return True

    def compress_old_entries(self, keep_recent: int = 5):
        """保留接口兼容。发言压缩由 compress_old_speeches 处理。"""
        pass

    # ── Prompt 格式化 ──

    def format_for_prompt(self) -> str:
        """格式化为 prompt 片段，按信息密度分层渲染。"""
        lines = [
            "## Working Memory (This Game)",
            f"Role: {self.my_role} | Seat: {self.my_seat} | Day: {self.day}",
        ]

        actions_text = self._format_game_actions()
        if actions_text:
            lines.append("\n### Game Actions")
            lines.append(actions_text)

        if self.known_info:
            lines.append("\n### Known Info")
            lines.extend(f"- {info}" for info in self.known_info[-10:])

        speech_lines = self._format_speeches()
        if speech_lines:
            lines.extend(speech_lines)

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

    # ── Private helpers ──

    def _add_speech(self, content: str, round_num: int, status: str, event: Dict):
        if not content:
            return
        speaker = self._extract_speaker(event)
        self.speeches.append(SpeechRecord(
            raw=content, day=round_num, phase=status, speaker=speaker or "",
        ))

    def _format_game_actions(self) -> str:
        if not self.game_actions:
            return ""

        by_day: Dict[int, List[GameAction]] = {}
        for a in self.game_actions:
            by_day.setdefault(a.day, []).append(a)

        day_lines = []
        for day in sorted(by_day.keys()):
            by_type: Dict[str, List[GameAction]] = {}
            for a in by_day[day]:
                by_type.setdefault(a.action_type, []).append(a)

            parts = []
            for action_type in _ACTION_DISPLAY_ORDER:
                type_actions = by_type.get(action_type, [])
                if not type_actions:
                    continue
                label, formatter = _ACTION_FORMATS[action_type]
                items = [formatter(a) for a in type_actions if formatter(a)]
                if items:
                    parts.append(f"{label}[{', '.join(items)}]")

            if parts:
                day_lines.append(f"D{day}: {' | '.join(parts)}")

        return "\n".join(day_lines)

    def _format_speeches(self) -> List[str]:
        if not self.speeches:
            return []

        cutoff = self.day - 1
        recent = [s for s in self.speeches if s.day >= cutoff]
        historical = [s for s in self.speeches if s.day < cutoff]

        lines = []

        if recent:
            recent_min = max(1, cutoff)
            if recent_min == self.day:
                label = f"Day {self.day}"
            else:
                label = f"Day {recent_min}-{self.day}"
            lines.append(f"\n### Recent Speeches ({label}, full text)")
            for s in sorted(recent, key=lambda s: (s.day, s.phase)):
                phase_label = SPEECH_PHASE_LABELS.get(s.phase, s.phase)
                speaker_label = f"{s.speaker}号" if s.speaker and s.speaker.isdigit() else (s.speaker or "?")
                lines.append(f"[D{s.day} {phase_label}] {speaker_label}: {s.raw}")

        if historical:
            historical_days = sorted(set(s.day for s in historical))
            day_range = f"Day {min(historical_days)}-{max(historical_days)}" if len(historical_days) > 1 else f"Day {historical_days[0]}"
            lines.append(f"\n### Historical Speeches ({day_range})")

            for day in historical_days:
                if day in self.day_summaries:
                    lines.append(f"--- D{day} Summary ---")
                    lines.append(self.day_summaries[day])
                else:
                    day_speeches = [s for s in historical if s.day == day]
                    for s in sorted(day_speeches, key=lambda s: s.phase):
                        phase_label = SPEECH_PHASE_LABELS.get(s.phase, s.phase)
                        speaker_label = f"{s.speaker}号" if s.speaker and s.speaker.isdigit() else (s.speaker or "?")
                        lines.append(f"[D{day} {phase_label}] {speaker_label}: {s.raw}")

        return lines

    def _extract_speaker(self, event: Dict) -> Optional[str]:
        extra = event.get("extra") or {}
        for key in ("speaker", "speaker_id", "player", "player_id", "from"):
            value = extra.get(key) or event.get(key)
            if value:
                return str(value)

        for trace in event.get("traces") or []:
            action = trace.get("action")
            if action in ("speak", "speech", "discussion", "last_words",
                          "sheriff_speech", "sheriff_pk_speech") and trace.get("from"):
                return str(trace["from"])

        content = str(event.get("content") or "")
        match = re.match(r"\s*(?:玩家)?(\d{1,2})\s*号?\s*[：:,，\s]", content)
        if match:
            return match.group(1)
        return None

    # ── 序列化 ──

    def to_dict(self) -> Dict:
        return {
            "game_id": self.game_id,
            "my_role": self.my_role,
            "my_seat": self.my_seat,
            "day": self.day,
            "known_info": self.known_info,
            "speeches": [s.to_dict() for s in self.speeches],
            "game_actions": [a.to_dict() for a in self.game_actions],
            "my_speeches": self.my_speeches,
            "contradictions": self.contradictions,
            "flags": self.flags,
            "suspicion": self.suspicion,
            "day_summaries": self.day_summaries,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "WorkingMemory":
        field_names = set(cls.__dataclass_fields__.keys())
        kwargs: Dict[str, Any] = {}

        for k, v in data.items():
            if k == "actions":
                # Migration: old "actions" → game_actions; can't parse strings, start fresh
                if "game_actions" not in data:
                    kwargs["game_actions"] = []
                continue

            if k == "game_actions":
                kwargs["game_actions"] = [
                    GameAction.from_dict(a) if isinstance(a, dict) else a for a in v
                ]
                continue

            if k == "speeches":
                if isinstance(v, list):
                    kwargs["speeches"] = [
                        SpeechRecord.from_dict(s) if isinstance(s, dict) else s for s in v
                    ]
                elif isinstance(v, dict):
                    # Migration: old format {"D1": {"发言#01 1号": "content"}}
                    records = []
                    for day_key, day_speeches in v.items():
                        day_num = int(day_key[1:]) if day_key.startswith("D") and day_key[1:].isdigit() else 1
                        for label, content in day_speeches.items():
                            speaker = ""
                            match = re.search(r"(\d+)号", label)
                            if match:
                                speaker = match.group(1)
                            records.append(SpeechRecord(
                                raw=content, day=day_num, phase="discussion", speaker=speaker,
                            ))
                    kwargs["speeches"] = records
                else:
                    kwargs["speeches"] = []
                continue

            if k in field_names:
                kwargs[k] = v

        return cls(**kwargs)
