"""In-memory per-session store with idle-based TTL expiry.

Replaces the LangGraph MemorySaver checkpointer. Each session_id owns an
independent AgentState dict; entries expire after `ttl_seconds` of inactivity
(sliding TTL — every read/write refreshes the deadline). A background task can
periodically call `cleanup_expired()` to release memory eagerly; reads also
evict on access so a stale session never leaks into a caller.
"""
import os
import threading
import time
from typing import Any, Dict, Optional


def _default_ttl() -> float:
    """TTL in seconds, configurable via SESSION_TTL_SECONDS (default 2h)."""
    try:
        return float(os.getenv("SESSION_TTL_SECONDS", "7200"))
    except ValueError:
        return 7200.0


class SessionStore:
    def __init__(self, ttl_seconds: Optional[float] = None):
        self.ttl = ttl_seconds if ttl_seconds is not None else _default_ttl()
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _deadline(self) -> float:
        return time.monotonic() + self.ttl

    def create(self, session_id: str, state: Dict[str, Any]) -> None:
        """Seed a brand-new session (overwrites any existing one)."""
        with self._lock:
            self._sessions[session_id] = {"state": state, "expires_at": self._deadline()}

    def set(self, session_id: str, state: Dict[str, Any]) -> None:
        """Persist updated state and refresh the TTL."""
        with self._lock:
            self._sessions[session_id] = {"state": state, "expires_at": self._deadline()}

    def get(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Return the session state, or None if missing/expired. Refreshes TTL."""
        with self._lock:
            entry = self._sessions.get(session_id)
            if entry is None:
                return None
            if entry["expires_at"] <= time.monotonic():
                del self._sessions[session_id]
                return None
            entry["expires_at"] = self._deadline()
            return entry["state"]

    def exists(self, session_id: str) -> bool:
        return self.get(session_id) is not None

    def delete(self, session_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

    def cleanup_expired(self) -> int:
        """Drop all expired sessions; returns the number removed."""
        now = time.monotonic()
        with self._lock:
            expired = [sid for sid, e in self._sessions.items() if e["expires_at"] <= now]
            for sid in expired:
                del self._sessions[sid]
        return len(expired)

    def active_count(self) -> int:
        self.cleanup_expired()
        with self._lock:
            return len(self._sessions)
