import tempfile
import unittest
from pathlib import Path

from scripts import preflight


class TestPreflight(unittest.TestCase):
    def test_find_agent_pin_ignores_commented_examples(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hermes.conf"
            path.write_text(";HERMES_AGENT_PIN=123456\n", encoding="utf-8")
            found, found_path = preflight._find_agent_pin(Path(tmp))
            self.assertFalse(found)
            self.assertIsNone(found_path)

    def test_find_agent_pin_detects_live_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "extensions.conf"
            path.write_text("[globals]\nHERMES_AGENT_PIN=654321\n", encoding="utf-8")
            found, found_path = preflight._find_agent_pin(Path(tmp))
            self.assertTrue(found)
            self.assertEqual(found_path, path)

    def test_check_asterisk_pin_gate_can_warn_without_pin(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hermes.conf"
            path.write_text(
                "\n".join(
                    [
                        ";HERMES_AGENT_PIN=123456",
                        "[hermes-agent-auth]",
                        "same => n,Authenticate(${HERMES_AGENT_PIN},,6)",
                        "[hermes]",
                        "same => n,Gosub(hermes-agent-auth,s,1)",
                        "same => n,Stasis(hermes)",
                    ]
                ),
                encoding="utf-8",
            )
            self.assertTrue(preflight.check_asterisk_pin_gate(Path(tmp)))
            self.assertFalse(
                preflight.check_asterisk_pin_gate(Path(tmp), require_agent_pin=True)
            )


if __name__ == "__main__":
    unittest.main()
