import json
import os
from urllib.parse import unquote
from typing import List, Dict, Any, Optional
from datetime import datetime

class PromptLogger:
    def __init__(self, log_file: str = "prompts_history.jsonl"):
        self.log_file = log_file
        self.history: List[Dict[str, Any]] = []
        self._load_from_file()

    def _load_from_file(self):
        """服务启动时，从磁盘恢复历史记录"""
        if os.path.exists(self.log_file):
            try:
                with open(self.log_file, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            self.history.append(json.loads(line))
                # 只保留最近 200 条，防止内存溢出
                self.history = self.history[-200:]
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
        
        # 实时写入磁盘 (JSONL 格式)
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
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
