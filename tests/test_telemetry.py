import json
import unittest

from src import telemetry


class TestTelemetry(unittest.TestCase):
    def test_elapsed_ms_uses_supplied_end(self):
        self.assertEqual(telemetry.elapsed_ms(10.0, end=10.1234), 123)

    def test_log_event_emits_parseable_json(self):
        with self.assertLogs("perf", level="INFO") as captured:
            telemetry.log_event(
                "agent.request",
                session_id="session-1",
                elapsed_ms=42,
                ok=True,
                backend="openai",
            )

        payload = captured.output[0].split("perf ", 1)[1]
        data = json.loads(payload)

        self.assertEqual(data["event"], "agent.request")
        self.assertEqual(data["session_id"], "session-1")
        self.assertEqual(data["elapsed_ms"], 42)
        self.assertTrue(data["ok"])
        self.assertEqual(data["backend"], "openai")

    def test_log_event_coerces_non_json_values(self):
        with self.assertLogs("perf", level="INFO") as captured:
            telemetry.log_event("stt.transcribe", payload=b"abc")

        payload = captured.output[0].split("perf ", 1)[1]
        data = json.loads(payload)

        self.assertEqual(data["payload"], "<3 bytes>")


if __name__ == "__main__":
    unittest.main()
