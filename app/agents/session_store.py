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
        self._aliases: Dict[str, str] = {}  # agent_id → session_id
        self._lock = threading.Lock()

    def _deadline(self) -> float:
        return time.monotonic() + self.ttl

    def create(self, session_id: str, state: Dict[str, Any],
               agent_id: Optional[str] = None) -> None:
        """Seed a brand-new session (overwrites any existing one).

        If *agent_id* is provided, registers an alias so that HTTP callers
        which only carry agent_id can still locate the session.
        """
        with self._lock:
            self._sessions[session_id] = {"state": state, "expires_at": self._deadline()}
            if agent_id:
                self._aliases[agent_id] = session_id

    def set(self, session_id: str, state: Dict[str, Any]) -> None:
        """Persist updated state and refresh the TTL."""
        with self._lock:
            self._sessions[session_id] = {"state": state, "expires_at": self._deadline()}

    def resolve_session_id(self, key: str) -> Optional[str]:
        """Resolve a key (session_id or agent_id) to the real session_id.

        If *key* is an agent_id alias, return the mapped session_id;
        otherwise return *key* itself (it may be a direct session_id).
        """
        with self._lock:
            return self._aliases.get(key, key)

    def get(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Return the session state, or None if missing/expired. Refreshes TTL.

        Automatically resolves agent_id aliases to session_id.
        """
        with self._lock:
            real_sid = self._aliases.get(session_id, session_id)
            entry = self._sessions.get(real_sid)
            if entry is None:
                return None
            if entry["expires_at"] <= time.monotonic():
                del self._sessions[real_sid]
                self._aliases = {k: v for k, v in self._aliases.items() if v != real_sid}
                return None
            entry["expires_at"] = self._deadline()
            return entry["state"]

    def exists(self, session_id: str) -> bool:
        return self.get(session_id) is not None

    def delete(self, session_id: str) -> bool:
        with self._lock:
            real_sid = self._aliases.get(session_id, session_id)
            self._aliases = {k: v for k, v in self._aliases.items() if v != real_sid}
            return self._sessions.pop(real_sid, None) is not None

    def cleanup_expired(self) -> int:
        """Drop all expired sessions; returns the number removed."""
        now = time.monotonic()
        with self._lock:
            expired = [sid for sid, e in self._sessions.items() if e["expires_at"] <= now]
            for sid in expired:
                del self._sessions[sid]
                self._aliases = {k: v for k, v in self._aliases.items() if v != sid}
        return len(expired)

    def active_count(self) -> int:
        self.cleanup_expired()
        with self._lock:
            return len(self._sessions)
