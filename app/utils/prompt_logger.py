import json
import os
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
            response: str = "", session_id: str = ""):
        entry = {
            "timestamp": datetime.now().isoformat(),
            "agent_id": agent_id,
            "session_id": session_id,
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

    def get_history(self, session_id: Optional[str] = None):
        if session_id:
            return [e for e in self.history if e.get("session_id") == session_id]
        return self.history

# 单例模式
prompt_logger = PromptLogger()
