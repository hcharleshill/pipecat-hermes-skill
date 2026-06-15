"""
Error Handler

Reusable error handling patterns from the Telegram skill.

Enhanced with traceback logging, optional session context, and helpers
for producing safe fallback messages. Retries are applied at call sites
(e.g. Hermes) rather than inside generic error handling.
"""

import logging
import traceback
from typing import Optional

logger = logging.getLogger(__name__)


def handle_error(
    error: Exception,
    context: str = "",
    session_id: Optional[str] = None,
    recoverable: bool = True,
) -> None:
    """
    Log an error with full traceback and optional structured context.

    Args:
        error: The exception that occurred.
        context: Short description of where/why (e.g. "routing message", "TTS synthesis").
        session_id: Optional session identifier for correlation in logs.
        recoverable: Whether this is considered a recoverable/expected transient failure.
                     Affects log level (WARNING vs ERROR) for monitoring purposes.
    """
    sid = f" [session={session_id}]" if session_id else ""
    level = logging.WARNING if recoverable else logging.ERROR
    logger.log(
        level,
        f"Error in {context}{sid}: {error}\n{traceback.format_exc()}"
    )


def get_user_friendly_message(error: Exception, context: str = "") -> str:
    """
    Return a safe, generic message suitable for returning to the end user
    when an internal failure occurs. Never leaks stack traces.
    """
    ctx = f" while {context}" if context else ""
    # Keep messages short and calm
    if "hermes" in context.lower() or "agent" in context.lower():
        return "I'm sorry, I had trouble reaching the agent right now."
    if "tts" in context.lower() or "speech" in context.lower():
        return "I have a response, but I'm having trouble speaking it right now."
    if "stt" in context.lower() or "transcri" in context.lower() or "audio" in context.lower():
        return "Sorry, I didn't catch that clearly."
    return f"I'm sorry, something went wrong{ctx}. Please try again."