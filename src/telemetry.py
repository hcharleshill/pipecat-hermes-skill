"""
Small performance telemetry helpers for the voice-agent call path.

This module intentionally stays dependency-free. It emits compact JSON on the
``perf`` logger so live-call logs can be grepped today and parsed later.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional


logger = logging.getLogger("perf")


def now() -> float:
    """Return a high-resolution monotonic timestamp for elapsed-time measurement."""
    return time.perf_counter()


def elapsed_ms(start: float, end: Optional[float] = None) -> int:
    """Return elapsed milliseconds from a timestamp produced by now()."""
    finish = now() if end is None else end
    return max(0, int(round((finish - start) * 1000)))


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return str(value)


def log_event(event: str, session_id: Optional[str] = None, **fields: Any) -> None:
    """
    Emit one structured performance event.

    Keep event fields free of caller transcript text or secrets. Sizes, counts,
    backend names, and durations are enough for the first latency pass.
    """
    payload: dict[str, Any] = {"event": event}
    if session_id is not None:
        payload["session_id"] = session_id
    payload.update(fields)
    logger.info(
        "perf %s",
        json.dumps(_json_safe(payload), sort_keys=True, separators=(",", ":")),
    )
