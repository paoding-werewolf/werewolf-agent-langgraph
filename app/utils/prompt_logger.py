import json
import os
from urllib.parse import unquote
from typing import List, Dict, Any, Optional
from datetime import datetime

MAX_HISTORY_ENTRIES = 200                 # 内存中最多保留的条数
TAIL_READ_BYTES = 512 * 1024              # 启动时只读文件尾部，避免大文件撑爆内存
MAX_LOG_FILE_BYTES = 512 * 1024 * 1024    # 磁盘文件超过此值时轮转为 .1

class PromptLogger:
    def __init__(self, log_file: str = "prompts_history.jsonl"):
        self.log_file = log_file
        self.history: List[Dict[str, Any]] = []
        self._load_from_file()

    def _load_from_file(self):
        """服务启动时，从磁盘尾部恢复最近的历史记录"""
        if not os.path.exists(self.log_file):
            return
        try:
            size = os.path.getsize(self.log_file)
            with open(self.log_file, "rb") as f:
                if size > TAIL_READ_BYTES:
                    f.seek(size - TAIL_READ_BYTES)
                    f.readline()  # 丢弃尾部起点处不完整的行
                tail = f.read().decode("utf-8", errors="replace")
            entries = []
            for line in tail.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except Exception:
                    continue
            self.history = entries[-MAX_HISTORY_ENTRIES:]
        except Exception:
            self.history = []

    def log(self, agent_id: str, phase: str, system_prompt: str, user_msg: str,
            response: str = "", session_id: str = "", external_agent_id: str = ""):
        entry = {
            "timestamp": datetime.now().isoformat(),
            "agent_id": agent_id,
            "session_id": session_id,
            "external_agent_id": external_agent_id,
            "phase": phase,
            "system_prompt": system_prompt,
            "user_msg": user_msg,
            "response": response
        }
        self.history.append(entry)
        if len(self.history) > MAX_HISTORY_ENTRIES:
            self.history = self.history[-MAX_HISTORY_ENTRIES:]

        # 实时写入磁盘 (JSONL 格式)，文件过大时先轮转
        try:
            self._rotate_if_needed()
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _rotate_if_needed(self):
        try:
            if os.path.exists(self.log_file) and os.path.getsize(self.log_file) > MAX_LOG_FILE_BYTES:
                os.replace(self.log_file, self.log_file + ".1")
        except Exception:
            pass

    def get_history(self, session_id: Optional[str] = None, external_agent_id: Optional[str] = None):
        result = self.history
        if session_id:
            result = [e for e in result if e.get("session_id") == session_id]
        if external_agent_id:
            result = [e for e in result if self._external_agent_matches(e, external_agent_id)]
        return result

    def get_history_by_external_agent(self, external_agent_id: str):
        return [e for e in self.history if self._external_agent_matches(e, external_agent_id)]

    def _external_agent_matches(self, entry: Dict[str, Any], query: str) -> bool:
        stored = self._normalize_external_agent_id(entry.get("external_agent_id"))
        wanted = self._normalize_external_agent_id(query)
        if not wanted:
            return False
        if stored == wanted:
            return True
        return self._conjugate_numeric_id(stored) == self._conjugate_numeric_id(wanted)

    def _normalize_external_agent_id(self, value: Any) -> str:
        return unquote(str(value or "").strip())

    def _conjugate_numeric_id(self, value: str) -> Optional[str]:
        if not value:
            return None
        if value.startswith("agent:"):
            suffix = value.split(":", 1)[1]
            return suffix if suffix.isdigit() else None
        return value if value.isdigit() else None

# 单例模式
prompt_logger = PromptLogger()
