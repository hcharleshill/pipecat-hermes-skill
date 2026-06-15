"""
Session Manager

Reused concept from the Telegram skill.
Manages conversation state across multiple sessions.

Supports optional file-based persistence (one JSON file per session) and
automatic timeout enforcement using the configured timeout_seconds.
This provides "proper session persistence".
"""

import json
import time
from pathlib import Path
from typing import Optional, Dict, Any


class SessionManager:
    def __init__(
        self,
        persist_dir: Optional[str] = None,
        timeout_seconds: int = 300,
    ):
        """
        :param persist_dir: Directory for JSON session files. If None, only
            in-memory (original behavior). Directory is created if needed.
        :param timeout_seconds: Inactive sessions older than this are expired
            on next access.
        """
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self.persist_dir: Optional[Path] = Path(persist_dir) if persist_dir else None
        self.timeout_seconds = timeout_seconds

        if self.persist_dir:
            self.persist_dir.mkdir(parents=True, exist_ok=True)

    def _session_path(self, session_id: str) -> Optional[Path]:
        if not self.persist_dir:
            return None
        # Safe filename
        safe_id = "".join(c for c in session_id if c.isalnum() or c in ("-", "_", "."))
        return self.persist_dir / f"{safe_id}.json"

    def _load_from_disk(self, session_id: str) -> Optional[Dict[str, Any]]:
        path = self._session_path(session_id)
        if not path or not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("id") == session_id:
                return data
        except Exception:
            # Corrupt or unreadable — start fresh
            pass
        return None

    def _save_to_disk(self, session: Dict[str, Any]) -> None:
        path = self._session_path(session.get("id", ""))
        if not path:
            return
        try:
            session["last_activity"] = time.time()
            with path.open("w", encoding="utf-8") as f:
                json.dump(session, f, indent=2, ensure_ascii=False)
        except Exception:
            # Best-effort persistence; never break the conversation
            pass

    def _is_expired(self, session: Dict[str, Any]) -> bool:
        if self.timeout_seconds <= 0:
            return True
        last = session.get("last_activity") or session.get("created_at", 0)
        return (time.time() - last) > self.timeout_seconds

    def get_or_create(self, session_id: str) -> Dict[str, Any]:
        """
        Get or create a session. Loads from disk (if persistence enabled),
        enforces timeout, and updates last_activity.
        """
        if session_id in self.sessions:
            session = self.sessions[session_id]
        else:
            session = self._load_from_disk(session_id)
            if session:
                self.sessions[session_id] = session

        if session and self._is_expired(session):
            session = None  # force fresh

        if not session:
            now = time.time()
            session = {
                "id": session_id,
                "history": [],
                "metadata": {},
                "created_at": now,
                "last_activity": now,
            }
            self.sessions[session_id] = session

        session["last_activity"] = time.time()
        self._save_to_disk(session)
        return session

    def update_and_persist(self, session: Dict[str, Any]) -> None:
        """Call this after mutating history/metadata to ensure disk is updated."""
        self._save_to_disk(session)

    def clear(self, session_id: str) -> None:
        if session_id in self.sessions:
            del self.sessions[session_id]
        path = self._session_path(session_id)
        if path and path.exists():
            try:
                path.unlink()
            except Exception:
                pass

    def cleanup_expired(self) -> list:
        """
        Evict expired sessions from memory + disk.
        Returns list of session_ids that were removed (empty list if none).
        """
        now = time.time()
        expired = []
        for sid, sess in list(self.sessions.items()):
            if self.timeout_seconds <= 0:
                expired.append(sid)
                continue
            last = sess.get("last_activity") or sess.get("created_at", 0)
            if (now - last) > self.timeout_seconds:
                expired.append(sid)
        for sid in expired:
            self.clear(sid)
        return expired

    def get_active_session_ids(self) -> list:
        self.cleanup_expired()
        return list(self.sessions.keys())
