"""
Basic unit tests for SessionManager (persistence + timeout).

These run with only stdlib (no ML models or external packages required).
"""

import json
import tempfile
import time
import unittest
from pathlib import Path

from src.session_manager import SessionManager


class TestSessionManager(unittest.TestCase):
    def test_in_memory_basic(self):
        sm = SessionManager()  # no persist
        s = sm.get_or_create("sess1")
        self.assertEqual(s["id"], "sess1")
        self.assertEqual(s["history"], [])
        s["history"].append({"role": "user", "content": "hi"})
        self.assertEqual(len(sm.get_or_create("sess1")["history"]), 1)

    def test_timeout_expires_session(self):
        sm = SessionManager(timeout_seconds=0)  # immediate expire
        s1 = sm.get_or_create("sess1")
        s1["history"].append({"role": "user", "content": "hello"})
        # Force a new access after "timeout"
        time.sleep(0.01)
        s2 = sm.get_or_create("sess1")
        self.assertEqual(s2["history"], [])  # should have been reset

    def test_persistence_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            sm1 = SessionManager(persist_dir=tmp, timeout_seconds=999)
            s = sm1.get_or_create("persist-test")
            s["history"].append({"role": "user", "content": "remember me"})
            sm1.update_and_persist(s)

            # New manager instance should load it from disk
            sm2 = SessionManager(persist_dir=tmp, timeout_seconds=999)
            loaded = sm2.get_or_create("persist-test")
            self.assertEqual(loaded["history"], [{"role": "user", "content": "remember me"}])

    def test_clear_removes_persisted_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            sm = SessionManager(persist_dir=tmp)
            sm.get_or_create("to-clear")
            path = next(Path(tmp).glob("*.json"))
            self.assertTrue(path.exists())
            sm.clear("to-clear")
            self.assertFalse(path.exists())

    def test_cleanup_expired(self):
        sm = SessionManager(timeout_seconds=0)
        sm.get_or_create("a")
        sm.get_or_create("b")
        removed = sm.cleanup_expired()
        self.assertEqual(len(removed), 2)
        self.assertIn("a", removed)
        self.assertIn("b", removed)
        self.assertEqual(sm.get_active_session_ids(), [])

    def test_get_active_session_ids(self):
        sm = SessionManager(timeout_seconds=999)
        sm.get_or_create("active1")
        sm.get_or_create("active2")
        ids = sm.get_active_session_ids()
        self.assertIn("active1", ids)
        self.assertIn("active2", ids)


if __name__ == "__main__":
    unittest.main()