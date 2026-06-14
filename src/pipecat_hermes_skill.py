"""
Pipecat Hermes Skill

This module reuses useful patterns from the Telegram skill (message routing,
session management, error handling) and adapts them for use with Pipecat
voice pipelines (STT + TTS).

The goal is to provide a clean interface between Hermes and Pipecat.
"""

import logging
import math
import os
import random
import re
import struct
import tempfile
import threading
import time
import wave
from typing import Callable, Optional, List

from . import config as config_module
from . import stt as stt_module
from . import tts as tts_module
from .thinking_verbs import long_wait_phrase_pool
from .error_handler import handle_error, get_user_friendly_message
from .session_manager import SessionManager

logger = logging.getLogger(__name__)


class PipecatHermesSkill:
    """
    Main class for routing messages between Hermes and Pipecat.

    Integrates:
      - Local STT (Faster-Whisper via stt.py)
      - Local TTS (Piper via tts.py)
      - Session management (via SessionManager)
      - Configuration (from config/config.yaml)
      - Error handling

    The hermes_client and pipecat_pipeline are injected for the text agent
    and audio transport respectively.
    """

    def __init__(self, hermes_client, pipecat_pipeline):
        self.hermes = hermes_client
        self.pipeline = pipecat_pipeline

        # Use shared config
        self.config = config_module.config

        # SessionManager now provides proper persistence + timeout.
        # Uses config.session.persist / persist_dir (defaults: True, "sessions").
        # Conversation history + metadata will survive restarts and respect timeout.
        session_cfg = self.config.session
        persist_dir = session_cfg.persist_dir if getattr(session_cfg, "persist", True) else None
        self.session_manager = SessionManager(
            persist_dir=persist_dir,
            timeout_seconds=session_cfg.timeout_seconds,
        )

        # Lock to support multiple concurrent sessions safely (threading).
        # Protects the transient per-session audio/speaking state dicts.
        # (SessionManager handles its own persistence concerns.)
        self._lock = threading.RLock()  # reentrant so nested calls (e.g. check -> process -> route) are safe

        # Simple lifecycle event hooks (added to address "full lifecycle events" gap).
        # External code (Asterisk bridge, Pipecat transport, tests) can register callbacks.
        self._on_session_end: list = []

        # STATE SEPARATION (important for concurrent sessions + persistence story):
        # Durable / persisted state lives in self.session_manager (history, metadata, timestamps).
        # Transient acoustic / real-time state stays here under the RLock for low-latency
        # access during audio streaming and turn detection:
        #   - audio_buffers + last_audio_time : raw chunks + VAD timing for turn boundaries
        #   - is_speaking + last_assistant_response : barge-in control + resume prompts
        # This hybrid design keeps SessionManager reusable and easily serializable while
        # the skill owns the time-sensitive, non-persistent voice state.
        # When a session fully ends we clean both.

        # Lightweight metrics / observability hooks (addresses one of the Future TODO items).
        # Counters + a couple of gauges. No external dependencies; query with get_metrics().
        # A production system can poll this or push to Prometheus/etc from the bridge.
        self._metrics = {
            "turns_processed": 0,
            "barge_ins": 0,
            "hermes_calls": 0,
            "hermes_errors": 0,
            "tts_syntheses": 0,
            "last_route_latency_ms": None,
        }

        # Audio chunk buffers per session (raw bytes, assumed consistent PCM format)
        # Format assumption: 16kHz, 16-bit, mono by default (common for Whisper/Piper)
        self.audio_buffers: dict[str, list[bytes]] = {}
        self.last_audio_time: dict[str, float] = {}

        # Interruption / barge-in state
        self.is_speaking: dict[str, bool] = {}
        # True while STT/Hermes/TTS is running for a committed turn (before playback ends)
        self.processing_turn: dict[str, bool] = {}
        # Per-session synthesized response audio (bridge reads this instead of a global)
        self.last_response_audio_by_session: dict[str, bytes] = {}
        self.last_assistant_response: dict[str, str] = {}

        # Whether we have recently seen speech energy for this session.
        # Used to avoid declaring "end of turn" while the user is actively speaking
        # (even if there are momentary low-energy gaps in the RTP stream).
        self.speech_active: dict[str, bool] = {}

        # Hard gate: ignore mic while agent audio is playing (prevents speaker bleed).
        self.agent_playback_active: dict[str, bool] = {}
        # Ignore mic until this timestamp (post-playback cooldown).
        self.mic_unmute_after: dict[str, float] = {}
        # Ignore turn detection until this timestamp (connect tone / call setup).
        self.turn_detect_after: dict[str, float] = {}

        # "speakerphone" = strict half-duplex + keyword interrupt only
        # "isolated"     = low echo detected (likely headphones) — energy barge-in OK
        self.acoustic_mode: dict[str, str] = {}
        self._interrupt_monitor_buffers: dict[str, list[bytes]] = {}
        self._interrupt_last_check: dict[str, float] = {}
        self._playback_interrupt_requested: dict[str, bool] = {}
        self.interrupt_keywords = re.compile(
            r"\b(stop|hold on|wait|cancel|never mind|nevermind)\b", re.IGNORECASE
        )
        self.echo_probe_rms_threshold = 175.0   # inbound energy while playing probe tone
        self.interrupt_monitor_interval = 0.55  # seconds between STT checks during playback
        self.interrupt_min_buffer_bytes = 24000  # ~0.75s @ 16kHz before checking keyword

        # Turn boundary thresholds (tunable)
        # - After a clear end-turn discourse marker (right?, over, you know?, etc.),
        #   we can cut the turn after a relatively short pause.
        # - For ordinary speech, wait for a longer pause before forcing a turn end.
        self.long_pause_threshold = 1.6          # wait for a clear end-of-thought pause
        self.short_pause_after_cue_threshold = 0.85   # after strong marker
        self.min_turn_bytes = 32000              # ~1.0s of speech @ 16kHz 16-bit mono
        self.post_playback_cooldown = 0.9        # default seconds after TTS before listening again
        self.post_playback_cooldown_by_session: dict[str, float] = {}
        self.session_start_grace = 2.0             # ignore STT right after call connects
        self.barge_in_enabled = False            # global default; per-session overridden by acoustic_mode

        # Lightweight verbal acknowledgements (short, low-cost TTS use only)
        # Played immediately on detected user turn end to say "I heard you"
        # without waiting for full Hermes response or heavy synthesis.
        # Avoid "Mhm." / "Hmm." — Piper (joe) often garbles them as "MHN".
        self.short_verbal_acks: List[str] = [
            "Alrighty!",
            "Gotcha.",
            "Well...",
            "Got it.",
            "Okay.",
            "Right.",
            "One moment.",
            "Let me see.",
            "Sure.",
        ]

        # Listening / computing filler: short tones on D natural minor (~2 octaves).
        # Played after the verbal ack while Hermes is still working. Procedural — no TTS cost.
        self.listening_bleep_tone_ms = 30
        self.listening_bleep_gap_range = (200, 200)   # ms pause between bleeps
        # D3 through D5, natural minor (equal temperament, A4=440 Hz)
        self.listening_bleep_scale_hz: List[float] = [
            146.83,  # D3
            164.81,  # E3
            174.61,  # F3
            196.00,  # G3
            220.00,  # A3
            233.08,  # Bb3
            261.63,  # C4
            293.66,  # D4
            329.63,  # E4
            349.23,  # F4
            392.00,  # G4
            440.00,  # A4
            466.16,  # Bb4
            523.25,  # C5
            587.33,  # D5
        ]
        self.listening_bleep_amplitude = 14000
        self.listening_bleep_cache_seconds = 10.0
        self._listening_bleeps_pcm_cache: Optional[bytes] = None

        # Spoken filler when Hermes is slow — Claude-style spinner verbs (+ apologies after 60s).
        self.long_wait_first_seconds = 7.0
        self.long_wait_interval_seconds = 7.5   # bridge uses random 7–8s between cues
        self.long_wait_interval_jitter = (7.0, 8.0)
        self.long_wait_max_repeats = 2          # same spinner phrase OK this many extra times
        self.long_wait_apology_after_seconds = 60.0

        self.call_greeting_text = (
            "Hello. This is Hermes. How may I help you today?"
        )

        # Apply logging level from config.
        # Use basicConfig only if no handlers are configured yet (avoids double-logging
        # when the caller or bridge has already set up logging).
        try:
            level_name = getattr(self.config.logging, "level", "INFO")
            level = getattr(logging, level_name.upper(), logging.INFO)
            if not logging.getLogger().handlers:
                logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
            logger.setLevel(level)
            # Also ensure child loggers from stt/tts/error_handler propagate at this level
            for mod_name in ("src.stt", "src.tts", "src.error_handler"):
                logging.getLogger(mod_name).setLevel(level)
        except Exception as exc:
            # Never let logging config break the skill
            logger.warning(f"Could not apply logging config: {exc}")

        logger.info("PipecatHermesSkill initialized (persistence + timeouts + improved error handling enabled)")

    def handle_incoming_audio(self, session_id: str, audio_chunk: bytes):
        """
        Append an incoming audio chunk for the given session.

        Updates activity time for turn-boundary detection.
        The recommended way for the transport layer to decide when to cut a turn is to
        periodically call check_for_end_of_turn(session_id).

        This implements the policy:
          - Short pause (~650ms) after a clear end-turn discourse marker ("right?",
            "over", "you know?", "got it?", tag questions, etc.) → end turn.
          - Otherwise wait for a longer pause (1200ms default) before ending the turn.

        Audio format: raw PCM bytes (we default to 16kHz mono 16-bit when
        creating the WAV for transcription).
        """
        if not audio_chunk:
            return

        with self._lock:
            if time.time() < self.turn_detect_after.get(session_id, 0.0):
                return
            if self.agent_playback_active.get(session_id):
                if self.acoustic_mode.get(session_id) == "isolated":
                    if self._chunk_has_speech_energy(audio_chunk, threshold=800):
                        self.on_user_barge_in(session_id)
                return
            if time.time() < self.mic_unmute_after.get(session_id, 0.0):
                return

            # While a turn is being processed (pre-playback), ignore mic audio for STT.
            if self.processing_turn.get(session_id):
                return
            if self.is_speaking.get(session_id):
                if (
                    self.acoustic_mode.get(session_id) == "isolated"
                    or self.barge_in_enabled
                ) and self._chunk_has_speech_energy(audio_chunk, threshold=800):
                    self.on_user_barge_in(session_id)
                return

            if session_id not in self.audio_buffers:
                self.audio_buffers[session_id] = []
                self.speech_active[session_id] = False
            self.audio_buffers[session_id].append(audio_chunk)

            has_energy = self._chunk_has_speech_energy(audio_chunk)
            if has_energy:
                self.last_audio_time[session_id] = time.time()
                self.speech_active[session_id] = True
                total_buf = sum(len(c) for c in self.audio_buffers[session_id])
                # Log energy only occasionally to avoid spam during long speech
                if total_buf % 16000 < 320:  # roughly every ~1s of audio
                    logger.info(f"[{session_id}] Energy detected, buffer ~{total_buf} bytes")

    def mark_session_start(self, session_id: str) -> None:
        """Called when a voice call connects; avoids transcribing setup tones."""
        with self._lock:
            now = time.time()
            self.turn_detect_after[session_id] = now + self.session_start_grace
            self.mic_unmute_after[session_id] = now + self.session_start_grace
            self.audio_buffers.pop(session_id, None)
            self.last_audio_time.pop(session_id, None)
            self.speech_active[session_id] = False
            self.acoustic_mode[session_id] = "speakerphone"
            self._interrupt_monitor_buffers.pop(session_id, None)
            self._interrupt_last_check.pop(session_id, None)

    def set_acoustic_mode(self, session_id: str, mode: str, echo_rms: Optional[float] = None) -> None:
        """
        Set per-call acoustic profile based on echo probe.

        speakerphone: strict half-duplex; say 'stop' / 'wait' to interrupt playback.
        isolated:     low echo — allow energy-based barge-in during agent speech.
        """
        if mode not in ("speakerphone", "isolated"):
            mode = "speakerphone"
        with self._lock:
            self.acoustic_mode[session_id] = mode
            self.post_playback_cooldown_by_session[session_id] = (
                0.5 if mode == "isolated" else 0.9
            )
        if echo_rms is not None:
            logger.info(
                f"[{session_id}] Acoustic profile: {mode} "
                f"(echo probe RMS={echo_rms:.0f}, threshold={self.echo_probe_rms_threshold:.0f})"
            )
        else:
            logger.info(f"[{session_id}] Acoustic profile: {mode}")
        if mode == "speakerphone":
            logger.info(
                f"[{session_id}] Strict half-duplex active — say 'stop' or 'wait' to interrupt"
            )
        else:
            logger.info(f"[{session_id}] Isolated audio — barge-in enabled")

    def should_monitor_playback(self, session_id: str) -> bool:
        """True when we should watch the mic during agent playback (keyword interrupt)."""
        with self._lock:
            return (
                self.agent_playback_active.get(session_id, False)
                and self.acoustic_mode.get(session_id) == "speakerphone"
            )

    def monitor_playback_audio(self, session_id: str, audio_chunk: bytes) -> bool:
        """
        In speakerphone mode, listen during agent playback for interrupt keywords only.
        Returns True when 'stop' / 'wait' / etc. is detected.
        """
        if not audio_chunk or not self.should_monitor_playback(session_id):
            return False
        if not self._chunk_has_speech_energy(audio_chunk, threshold=300):
            return False

        with self._lock:
            buf = self._interrupt_monitor_buffers.setdefault(session_id, [])
            buf.append(audio_chunk)
            total = sum(len(c) for c in buf)
            now = time.time()
            last = self._interrupt_last_check.get(session_id, 0.0)
            if total < self.interrupt_min_buffer_bytes:
                return False
            if (now - last) < self.interrupt_monitor_interval:
                return False
            self._interrupt_last_check[session_id] = now
            snapshot = list(buf)
            self._interrupt_monitor_buffers[session_id] = []

        text = self._transcribe_buffer(snapshot)
        if not text:
            return False
        if self.interrupt_keywords.search(text):
            logger.info(f"[{session_id}] Keyword interrupt detected: {text!r}")
            return True
        return False

    def consume_playback_interrupt(self, session_id: str) -> bool:
        """Returns True once if the transport should stop RTP playback."""
        with self._lock:
            if self._playback_interrupt_requested.pop(session_id, False):
                return True
            return False

    def on_keyword_interrupt(self, session_id: str) -> None:
        """User said 'stop' during playback — yield the floor."""
        with self._lock:
            self._playback_interrupt_requested[session_id] = True
            self.agent_playback_active[session_id] = False
            self.is_speaking[session_id] = False
            self.processing_turn[session_id] = False
            self.mic_unmute_after[session_id] = time.time() + 0.2
            self._interrupt_monitor_buffers.pop(session_id, None)
            self._interrupt_last_check.pop(session_id, None)
            self.audio_buffers.pop(session_id, None)
            self.last_audio_time.pop(session_id, None)
            self.speech_active[session_id] = False
        logger.info(f"[{session_id}] Playback interrupted by keyword — listening for your turn")

    def _playback_cooldown(self, session_id: str) -> float:
        return self.post_playback_cooldown_by_session.get(
            session_id, self.post_playback_cooldown
        )

    def begin_agent_playback(self, session_id: str) -> None:
        """Block STT while agent audio is being sent to the caller."""
        with self._lock:
            self.agent_playback_active[session_id] = True
            self.is_speaking[session_id] = True
            self.audio_buffers.pop(session_id, None)
            self.last_audio_time.pop(session_id, None)
            self.speech_active[session_id] = False
            self._interrupt_monitor_buffers.pop(session_id, None)
            self._interrupt_last_check.pop(session_id, None)

    def end_agent_playback(self, session_id: str) -> None:
        """Re-open the mic after playback with a short cooldown."""
        with self._lock:
            self.agent_playback_active[session_id] = False
            self.is_speaking[session_id] = False
            unmute_at = time.time() + self._playback_cooldown(session_id)
            self.mic_unmute_after[session_id] = unmute_at
            self.turn_detect_after[session_id] = max(
                self.turn_detect_after.get(session_id, 0.0),
                unmute_at,
            )
            self.audio_buffers.pop(session_id, None)
            self.last_audio_time.pop(session_id, None)
            self.speech_active[session_id] = False

    def _is_meaningful_transcript(self, text: Optional[str]) -> bool:
        """Drop punctuation noise and ultra-short fragments before calling Hermes."""
        if not text:
            return False
        cleaned = " ".join(text.strip().split())
        if not cleaned:
            return False
        if re.match(r"^[\s.,!?;:'\"-]+$", cleaned):
            return False
        alpha = re.sub(r"[^A-Za-z]", "", cleaned)
        if len(alpha) < 3:
            return False
        words = [w for w in re.split(r"\s+", cleaned) if re.search(r"[A-Za-z]", w)]
        if not words:
            return False
        if len(words) == 1 and len(alpha) < 4:
            return False
        return True

    def _chunk_has_speech_energy(self, chunk: bytes, threshold: int = 100) -> bool:
        """Very lightweight energy detector for 16-bit PCM."""
        if not chunk or len(chunk) < 2:
            return False
        # Treat as signed 16-bit little-endian samples
        try:
            # Sample a few values for speed
            samples = struct.unpack("<" + "h" * (len(chunk) // 2), chunk[: min(len(chunk), 2000)])
            if not samples:
                return False
            rms = (sum(s * s for s in samples) / len(samples)) ** 0.5
            return rms > threshold
        except Exception:
            return False

    # --- Turn-boundary detection ---
    # Policy:
    #   - If the user utters a clear end-turn discourse marker (tag question,
    #     "over", "you know?", etc.) then a relatively *short* pause is enough
    #     to decide the turn is over.
    #   - For normal speech with no such marker, wait for a longer pause
    #     (default 1200ms) before forcing the turn to end.

    def is_turn_complete(self, session_id: str, silence_threshold_seconds: Optional[float] = None) -> bool:
        """
        Returns True if enough silence has elapsed since the last audio chunk.
        Uses the long pause threshold by default (for utterances without markers).
        """
        if session_id not in self.audio_buffers or not self.audio_buffers[session_id]:
            return False
        if silence_threshold_seconds is None:
            silence_threshold_seconds = self.long_pause_threshold
        last = self.last_audio_time.get(session_id, 0.0)
        return (time.time() - last) >= silence_threshold_seconds

    def _text_has_turn_cue(self, text: str) -> bool:
        """
        Detect end-turn discourse markers / tag questions in the transcribed text.
        A short pause after any of these is usually sufficient to end the speaker's turn.
        """
        if not text:
            return False
        t = text.lower().strip().rstrip(".!?,")

        # Explicit handoff (radio/voice style)
        if t == "over" or t.endswith(" over"):
            return True

        # Comprehensive list of common turn-yielding tag questions and cues
        turn_cues = {
            # Short high-confidence tags
            "right?", "yeah?", "yes?", "no?", "correct?", "true?",
            "okay?", "ok?", "kay?", "alright?", "all right?",
            "got it?", "clear?", "see?", "understand?", "follow?",
            "cool?", "good?", "fair?", "sound good?", "deal?",

            # Common conversational / validation tags
            "you know?", "y'know?", "ya know?",
            "know what i mean?", "know what i'm saying?", "know'm sayin'?",
            "make sense?", "does that make sense?",
            "see what i mean?", "see what i'm saying?",
            "you with me?", "you feel me?", "feel me?",
            "am i right?", "isn't that right?",
            "wouldn't you say?", "don't you think?",
            "or what?", "or no?", "fair enough?",
            "what do you think?", "thoughts?", "your thoughts?",
            "any questions?", "sound good to you?", "works for you?", "up to you",

            # Regional / stylistic
            "innit?", "eh?", "huh?",
            "you heard?",

            # Discourse markers that frequently yield the turn when followed by pause
            "that's it", "i'm done", "end of story", "period",
            "that's all", "moving on",
            "well", "hmm",
        }

        for cue in turn_cues:
            # Support both "right?" (if punct survived) and "right" (after rstrip)
            bare = cue.rstrip("?")
            if t.endswith(cue) or t.endswith(bare):
                return True

        return False

    def process_if_turn_complete(self, session_id: str, silence_threshold_seconds: Optional[float] = None) -> Optional[str]:
        """
        Legacy/simple helper: process the turn only if the (long) silence threshold
        has been reached. For the smarter marker-aware behavior, prefer
        check_for_end_of_turn().
        """
        if self.is_turn_complete(session_id, silence_threshold_seconds):
            return self.process_audio_turn(session_id)
        return None

    def check_for_end_of_turn(self, session_id: str) -> Optional[str]:
        """
        Primary method the audio transport should call periodically (e.g. every 200-300ms).

        Implements the requested policy:
          - Short pause after an end-turn discourse marker ("right?", "over",
            "you know?", tag questions, etc.) → end the turn.
          - Otherwise → wait until a long pause (default 1200ms) is detected.

        Returns the transcribed text if a turn was ended, otherwise None.

        Recommended flow for the transport (to avoid dead air):
          1. text = skill.check_for_end_of_turn(session_id)
          2. if text:
                 ack_audio = skill.get_turn_acknowledgement()
                 if ack_audio:
                     pipeline.send_audio(session_id, ack_audio)   # "Gotcha.", "Hmm.", "Well..."
          3. Kick off Hermes call (via route_message or directly).
          4. If the response is not ready after ~800ms, start sending short chunks
             from skill.generate_listening_bleeps(...) — rapid 5ms clicks with
             random 15-30ms gaps and 900-2000Hz pitch at low amplitude (gears
             turning / mechanical computing sound) to fill the gap. Keep looping
             at low volume until the final TTS response audio is ready to play.
        """
        with self._lock:
            if time.time() < self.turn_detect_after.get(session_id, 0.0):
                return None
            if self.agent_playback_active.get(session_id):
                return None
            if time.time() < self.mic_unmute_after.get(session_id, 0.0):
                return None
            if self.is_speaking.get(session_id) or self.processing_turn.get(session_id):
                return None

            buffer = self.audio_buffers.get(session_id)
            if not buffer:
                return None

            last_audio = self.last_audio_time.get(session_id, 0.0)
            if last_audio <= 0:
                return None
            silence = time.time() - last_audio

            total_buf = sum(len(c) for c in buffer)
            logger.debug(
                f"[{session_id}] check_for_end_of_turn: silence={silence:.2f}s "
                f"active={self.speech_active.get(session_id, False)} buffer={total_buf} bytes"
            )

            # Gate on speech_active: only consider ending a turn after we have observed
            # a period of silence following actual speech energy. This prevents
            # chopping an utterance into many tiny "turns" on internal low-energy gaps.
            if self.speech_active.get(session_id, False):
                if silence > 1.0:  # observed sustained post-speech silence (forgiving of natural pauses in speech)
                    self.speech_active[session_id] = False
                else:
                    return None

            # 1. Long pause → always end the turn (no marker required)
            if silence >= self.long_pause_threshold:
                # Require a minimum amount of audio in the buffer before declaring
                # a long-pause turn. This avoids processing 20ms fragments when
                # energy detection has gaps.
                total_bytes = sum(len(c) for c in buffer)
                if total_bytes < self.min_turn_bytes:
                    return None
                logger.debug(f"Long pause ({silence:.2f}s) for session {session_id} → ending turn")
                self.speech_active[session_id] = False
                return self.process_audio_turn(session_id, route=False)

            # 2. Short pause window → only end early if we can confirm a discourse marker
            if silence >= self.short_pause_after_cue_threshold:
                # Peek at what the current audio sounds like (transcribe without committing)
                current_buffer_copy = list(buffer)
                text = self._transcribe_buffer(current_buffer_copy)

                if text and self._text_has_turn_cue(text) and self._is_meaningful_transcript(text):
                    total_bytes = sum(len(c) for c in buffer)
                    if total_bytes < self.min_turn_bytes:
                        return None
                    logger.debug(
                        f"Turn cue + short pause ({silence:.2f}s) for session {session_id} → ending turn early"
                    )
                    self.audio_buffers.pop(session_id, None)
                    self.speech_active[session_id] = False
                    self.processing_turn[session_id] = True
                    self._metrics["turns_processed"] = self._metrics.get("turns_processed", 0) + 1
                    return text

                # No strong cue detected → do not end the turn yet.
                # Keep accumulating audio and wait for the long pause.
                return None

            return None

    def _transcribe_buffer(self, buffer: list[bytes]) -> Optional[str]:
        """
        Core transcription helper. Writes the given PCM chunks to a temp WAV
        and runs the local STT model. Returns the text or None on failure.
        Does NOT modify session state, clear buffers, or call route_message.
        """
        if not buffer:
            return None

        wav_path = None
        try:
            fd, wav_path = tempfile.mkstemp(suffix=".wav")
            os.close(fd)

            # Assumptions: 16 kHz, mono, 16-bit PCM (should match the transport)
            sample_rate = 16000
            channels = 1
            sample_width = 2

            with wave.open(wav_path, "wb") as wf:
                wf.setnchannels(channels)
                wf.setsampwidth(sample_width)
                wf.setframerate(sample_rate)
                for chunk in buffer:
                    wf.writeframes(chunk)

            text = stt_module.transcribe(wav_path)
            logger.debug(f"STT result: {text!r}")
            return text.strip() if text else None

        except Exception as e:
            handle_error(e, "buffer transcription", session_id=None)
            return None
        finally:
            if wav_path and os.path.exists(wav_path):
                try:
                    os.unlink(wav_path)
                except Exception:
                    pass

    # --- Interruption / Barge-in handling (high-priority TODO item) ---

    def on_user_barge_in(self, session_id: str):
        """
        Called (automatically via energy detection in handle_incoming_audio, or
        manually by the transport) when the user starts speaking while the
        assistant is responding.

        Basic behavior per spec:
          - Stop treating current audio as active
          - A short "Go ahead." acknowledgement can be generated by the caller
            (or we can synthesize one here if desired).
        The conversation history already contains the partial assistant turn.
        The next user message (after their turn) will naturally continue the context.
        """
        with self._lock:
            was_speaking = self.is_speaking.get(session_id, False)
            self.is_speaking[session_id] = False
            if was_speaking or self.agent_playback_active.get(session_id, False):
                self._playback_interrupt_requested[session_id] = True
                self.agent_playback_active[session_id] = False

            if was_speaking:
                logger.info(f"Barge-in detected on session {session_id}")
                self._metrics["barge_ins"] = self._metrics.get("barge_ins", 0) + 1
                # The transport layer should stop / duck the current TTS playback.
                # We keep the previous assistant text in last_assistant_response so
                # a future "resume" helper can use it.

    def get_barge_in_acknowledgement(self, session_id: str) -> Optional[bytes]:
        """
        Returns synthesized audio bytes for a short "Go ahead." ack.
        The caller (transport) can play this immediately on barge-in.
        """
        try:
            return tts_module.synthesize_to_wav_bytes("Go ahead.")
        except Exception as e:
            handle_error(e, "barge-in ack TTS", session_id=session_id)
            return None

    def get_call_greeting_audio(self) -> Optional[bytes]:
        """Opening greeting played once when a call connects."""
        try:
            return tts_module.synthesize_to_wav_bytes(self.call_greeting_text)
        except Exception as e:
            handle_error(e, "call greeting synthesis")
            return None

    def get_resume_prompt(self, session_id: str) -> str:
        """
        Returns text the caller can feed back to Hermes (or speak) to resume
        after a barge-in: "As I was saying. " + previous assistant response.
        """
        prev = self.last_assistant_response.get(session_id, "")
        if prev:
            return f"As I was saying. {prev}"
        return ""

    # --- Lightweight turn acknowledgements & listening noises ---
    # These exist so we never have dead air. We want to quickly signal
    # "I heard the turn" and then fill gaps with low-cost sounds while
    # the heavier Hermes response + TTS is being prepared.

    def get_turn_acknowledgement(self, session_id: Optional[str] = None) -> Optional[bytes]:
        """
        Returns a very short verbal acknowledgement audio (e.g. "Gotcha.",
        "Alrighty!", "Well...", "One moment.").

        These are intentionally short and lightweight so they don't overburden
        the TTS model. Play this immediately when check_for_end_of_turn()
        succeeds to tell the user their turn was accepted.

        The transport can play this, then (if the response is slow > ~800ms)
        follow up with rapid low-volume clicks from generate_listening_bleeps()
        ("gears turning" / computing sound) until the real response audio is ready.
        """
        if not self.short_verbal_acks:
            return None
        try:
            phrase = random.choice(self.short_verbal_acks)
            data = tts_module.synthesize_to_wav_bytes(phrase)
            logger.debug(f"Turn ack chosen: {phrase}")
            return data
        except Exception as e:
            handle_error(e, "turn acknowledgement synthesis", session_id=session_id)
            # Fallback: at least return a short burst of rapid clicks with randomized gaps ("still here")
            return self.get_listening_bleeps_slice(duration_seconds=0.35)

    def warm_listening_bleeps_cache(self, force: bool = False) -> bytes:
        """
        Render ~10s of thinking bleeps once (fixed seed) and reuse on every call.

        The Asterisk bridge loops this clip while Hermes works instead of
        regenerating random tones per chunk.
        """
        with self._lock:
            if self._listening_bleeps_pcm_cache is not None and not force:
                return self._listening_bleeps_pcm_cache

        duration = getattr(self, "listening_bleep_cache_seconds", 10.0)
        state = random.getstate()
        try:
            random.seed(42)
            pcm = self._render_listening_bleeps(duration_seconds=duration)
        finally:
            random.setstate(state)

        with self._lock:
            self._listening_bleeps_pcm_cache = pcm
        logger.info(
            f"Thinking bleep loop cached ({duration:.0f}s, {len(pcm)} PCM bytes @ 16kHz)"
        )
        return pcm

    def get_listening_bleeps_loop(self) -> bytes:
        """Return the cached ~10s thinking-bleep loop (16 kHz PCM)."""
        return self.warm_listening_bleeps_cache()

    def get_listening_bleeps_slice(
        self,
        duration_seconds: float,
        sample_rate: int = 16000,
    ) -> bytes:
        """Return the opening slice of the cached loop (for short fallbacks)."""
        loop = self.get_listening_bleeps_loop()
        if not loop:
            return b""
        nbytes = int(duration_seconds * sample_rate) * 2
        return loop[: min(nbytes, len(loop))]

    def get_long_wait_acknowledgement(
        self,
        session_id: Optional[str] = None,
        last_phrase: Optional[str] = None,
        last_audio: Optional[bytes] = None,
        repeat_count: int = 0,
        wait_elapsed_seconds: float = 0.0,
    ) -> Optional[tuple]:
        """
        Returns (phrase, wav_bytes) when Hermes has been working a long time.

        The transport should play the first phrase after long_wait_first_seconds,
        then another every ~7–8s until the response is ready.

        The same phrase may repeat up to long_wait_max_repeats times (reuses cached
        audio — no extra Piper call). New phrases are stored in the global TTS cache
        so later calls and turns reuse synthesized WAV bytes.

        Before long_wait_apology_after_seconds (~60s): Claude-style spinner verbs only.
        After that: verbs plus apology phrases ("Sorry for the wait.", "Sorry, still pondering.", …).
        """
        apology_after = getattr(self, "long_wait_apology_after_seconds", 60.0)
        max_repeats = getattr(self, "long_wait_max_repeats", 2)
        phrases = long_wait_phrase_pool(wait_elapsed_seconds, apology_after)
        if not phrases:
            return None
        try:
            if (
                last_phrase
                and last_audio
                and repeat_count < max_repeats
                and random.random() < 0.55
            ):
                logger.debug(f"Long-wait repeat ({repeat_count + 1}/{max_repeats}): {last_phrase}")
                return last_phrase, last_audio

            phrase = random.choice(phrases)
            data = tts_module.synthesize_to_wav_bytes(phrase)
            logger.debug(f"Long-wait phrase chosen: {phrase}")
            return phrase, data
        except Exception as e:
            handle_error(e, "long-wait acknowledgement synthesis", session_id=session_id)
            return None

    def generate_listening_bleeps(
        self,
        duration_seconds: float = 1.5,
        sample_rate: int = 16000,
    ) -> bytes:
        """
        Generates short thinking tones on a D natural minor scale (~2 octaves).

        Prefer get_listening_bleeps_loop() in production — the bridge caches and
        loops a fixed ~10s render. This method remains for ad-hoc / test use.
        """
        if duration_seconds <= 0:
            return b""
        return self._render_listening_bleeps(duration_seconds, sample_rate)

    def _render_listening_bleeps(
        self,
        duration_seconds: float,
        sample_rate: int = 16000,
    ) -> bytes:
        """Procedural D-minor thinking tones (16-bit mono PCM)."""
        num_samples = int(duration_seconds * sample_rate)
        audio = bytearray()

        # Pull current tuning from the skill instance so it can be adjusted live
        tone_ms = getattr(self, "listening_bleep_tone_ms", 30)
        gap_range = getattr(self, "listening_bleep_gap_range", (200, 200))
        scale = getattr(self, "listening_bleep_scale_hz", None) or [440.0]
        amplitude = getattr(self, "listening_bleep_amplitude", 14000)

        tone_samples = max(1, int(tone_ms / 1000.0 * sample_rate))
        attack_samples = max(2, int(0.004 * sample_rate))   # 4 ms fade-in
        release_samples = max(2, int(0.006 * sample_rate))  # 6 ms fade-out

        t = 0.0
        while len(audio) < num_samples * 2:
            freq = random.choice(scale)

            for i in range(tone_samples):
                if len(audio) >= num_samples * 2:
                    break

                # Smooth attack + exponential decay (no hard note edges)
                if i < attack_samples:
                    env = (i / attack_samples) ** 2
                else:
                    decay_pos = (i - attack_samples) / max(
                        tone_samples - attack_samples - release_samples, 1
                    )
                    env = math.exp(-3.5 * decay_pos)
                if i >= tone_samples - release_samples:
                    tail = tone_samples - 1 - i
                    env *= (tail / release_samples) ** 2

                sample = int(amplitude * env * math.sin(2 * math.pi * freq * t))
                audio.extend(struct.pack("<h", sample))
                t += 1.0 / sample_rate

            gap_ms = random.uniform(*gap_range)
            gap_samples = max(0, int(gap_ms / 1000.0 * sample_rate))
            for _ in range(gap_samples):
                if len(audio) >= num_samples * 2:
                    break
                audio.extend(struct.pack("<h", 0))
                t += 1.0 / sample_rate

        # Fade chunk boundaries so looping chunks do not click
        chunk_fade_samples = max(2, int(0.004 * sample_rate))
        total_samples = len(audio) // 2
        for i in range(min(chunk_fade_samples, total_samples // 2)):
            fade_in = i / chunk_fade_samples
            fade_out = (chunk_fade_samples - 1 - i) / chunk_fade_samples
            idx_in = i * 2
            idx_out = (total_samples - 1 - i) * 2
            s_in = struct.unpack_from("<h", audio, idx_in)[0]
            s_out = struct.unpack_from("<h", audio, idx_out)[0]
            struct.pack_into("<h", audio, idx_in, int(s_in * fade_in))
            struct.pack_into("<h", audio, idx_out, int(s_out * fade_out))

        return bytes(audio[: num_samples * 2])

    # --- Session lifecycle and concurrency helpers (Medium priority TODO completion) ---

    def add_on_session_end(self, callback):
        """
        Register a callback to be invoked when a session ends.
        Callback signature: callback(session_id: str) -> None
        Useful for transports to clean up per-call resources.
        Thread-safe registration.
        """
        with self._lock:
            if callback not in self._on_session_end:
                self._on_session_end.append(callback)

    def end_session(self, session_id: str) -> None:
        """
        Fully terminate a session:
          - Remove transient audio / speaking state (under lock)
          - Clear from SessionManager (memory + disk if persisted)
          - Fire any registered on_session_end callbacks
        Safe to call multiple times.
        """
        with self._lock:
            # Clean transient state
            self.audio_buffers.pop(session_id, None)
            self.last_audio_time.pop(session_id, None)
            self.is_speaking.pop(session_id, None)
            self.processing_turn.pop(session_id, None)
            self.last_assistant_response.pop(session_id, None)
            self.last_response_audio_by_session.pop(session_id, None)
            self.speech_active.pop(session_id, None)
            self.agent_playback_active.pop(session_id, None)
            self.mic_unmute_after.pop(session_id, None)
            self.turn_detect_after.pop(session_id, None)
            self.acoustic_mode.pop(session_id, None)
            self.post_playback_cooldown_by_session.pop(session_id, None)
            self._interrupt_monitor_buffers.pop(session_id, None)
            self._interrupt_last_check.pop(session_id, None)
            self._playback_interrupt_requested.pop(session_id, None)

            # Notify listeners (outside the per-session dicts but while lock held briefly)
            callbacks = list(self._on_session_end)

        # Clear durable state (no lock needed; SessionManager manages its own)
        self.session_manager.clear(session_id)

        for cb in callbacks:
            try:
                cb(session_id)
            except Exception as e:
                handle_error(e, f"on_session_end callback for {session_id}", session_id=session_id)

        logger.info(f"Session {session_id} ended and cleaned up")

    def _cleanup_transient_for_expired(self, expired_ids: list[str]) -> None:
        """Internal: called after manager cleanup to keep transient dicts in sync."""
        if not expired_ids:
            return
        with self._lock:
            for sid in expired_ids:
                self.audio_buffers.pop(sid, None)
                self.last_audio_time.pop(sid, None)
                self.is_speaking.pop(sid, None)
                self.processing_turn.pop(sid, None)
                self.last_assistant_response.pop(sid, None)
                self.last_response_audio_by_session.pop(sid, None)
                self.speech_active.pop(sid, None)
                self.agent_playback_active.pop(sid, None)
                self.mic_unmute_after.pop(sid, None)
                self.turn_detect_after.pop(sid, None)
                self.acoustic_mode.pop(sid, None)
                self.post_playback_cooldown_by_session.pop(sid, None)
                self._interrupt_monitor_buffers.pop(sid, None)
                self._interrupt_last_check.pop(sid, None)
        logger.debug(f"Cleaned transient state for expired sessions: {expired_ids}")

    def cleanup_expired_sessions(self) -> int:
        """
        Run expiration across durable (SessionManager) + transient state.
        Returns the number of sessions that were cleaned up.
        Transports can call this on a timer or on channel events.
        """
        removed = self.session_manager.cleanup_expired()
        self._cleanup_transient_for_expired(removed)
        return len(removed)

    def get_metrics(self) -> dict:
        """Return a snapshot of internal counters and last-seen values. Safe to call anytime."""
        with self._lock:
            return dict(self._metrics)

    def finish_agent_turn(self, session_id: str) -> None:
        """Called by the transport after agent audio playback completes."""
        with self._lock:
            self.agent_playback_active[session_id] = False
            self.is_speaking[session_id] = False
            self.processing_turn[session_id] = False
            self.mic_unmute_after[session_id] = (
                time.time() + self._playback_cooldown(session_id)
            )
            self.audio_buffers.pop(session_id, None)
            self.last_audio_time.pop(session_id, None)
            self.speech_active[session_id] = False

    def process_audio_turn(self, session_id: str, route: bool = True) -> Optional[str]:
        """
        Transcribe the accumulated audio buffer for this session using the
        local STT module. When route=True, also call route_message().

        Clears the audio buffer. This is the "commit the turn" path.
        The Asterisk bridge uses route=False so it can play ack + bleeps first.
        """
        with self._lock:
            if self.is_speaking.get(session_id) or self.processing_turn.get(session_id):
                return None
            buffer = self.audio_buffers.pop(session_id, [])
            if not buffer:
                return None
            self.processing_turn[session_id] = True
            self.speech_active[session_id] = False

        text = self._transcribe_buffer(buffer)

        if text and self._is_meaningful_transcript(text):
            logger.info(f"Processed audio turn for session {session_id} ({len(text)} chars)")
            with self._lock:
                self._metrics["turns_processed"] = self._metrics.get("turns_processed", 0) + 1
            if route:
                self.route_message(session_id, text)
        else:
            if text:
                logger.info(
                    f"Discarding low-quality transcript for session {session_id}: {text!r}"
                )
            else:
                logger.warning(f"Audio turn for session {session_id} produced no transcription")
            with self._lock:
                self.processing_turn[session_id] = False
            text = None

        return text

    def route_message(
        self,
        session_id: str,
        message: str,
        alive_check: Optional[Callable[[], bool]] = None,
    ):
        """
        Routes an incoming (transcribed) message to Hermes and handles the response.

        - Uses SessionManager for session state
        - Sends text to Hermes via the injected client (with retries)
        - Synthesizes the response with the local Piper TTS module
        - Attempts to deliver audio via the injected pipecat_pipeline

        alive_check: optional callable (from the voice bridge) — when it returns
        False the call has hung up and Hermes/TTS work is skipped.
        """
        logger.info(f"Routing message for session {session_id}: {message!r}")

        if alive_check and not alive_check():
            logger.info(f"Skipping route for {session_id} — call no longer active")
            return

        # Use the proper session manager (create if needed)
        session = self.session_manager.get_or_create(session_id)
        session["history"].append({"role": "user", "content": message})

        start = time.time()
        try:
            response = self._send_to_hermes(message, history=list(session["history"]))
            self._metrics["hermes_calls"] = self._metrics.get("hermes_calls", 0) + 1
        except Exception as e:
            handle_error(e, "routing message", session_id=session_id)
            self._metrics["hermes_errors"] = self._metrics.get("hermes_errors", 0) + 1
            response = get_user_friendly_message(e, "contacting the agent")

        if alive_check and not alive_check():
            logger.info(
                f"Discarding Hermes response for {session_id} — call ended during request"
            )
            return

        latency_ms = int((time.time() - start) * 1000)
        self._metrics["last_route_latency_ms"] = latency_ms

        if not response:
            response = "I don't have a response right now."

        logger.info(
            f"Hermes response for session {session_id} ({latency_ms}ms): "
            f"{response[:120]!r}{'...' if len(response) > 120 else ''}"
        )
        session["history"].append({"role": "assistant", "content": response})

        # Persist the updated conversation state (history + activity timestamp)
        self.session_manager.update_and_persist(session)

        if alive_check and not alive_check():
            logger.info(f"Skipping TTS for {session_id} — call ended before synthesis")
            return

        # Synthesize response to audio using local TTS
        audio_bytes: Optional[bytes] = None
        tts_path = None
        try:
            fd, tts_path = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            tts_module.synthesize(response, tts_path, use_cache=False)

            if alive_check and not alive_check():
                logger.info(f"Discarding TTS for {session_id} — call ended during synthesis")
                return

            with open(tts_path, "rb") as f:
                audio_bytes = f.read()

            logger.debug(f"TTS generated for session {session_id}, {len(audio_bytes)} bytes")
            self._metrics["tts_syntheses"] = self._metrics.get("tts_syntheses", 0) + 1
        except Exception as e:
            handle_error(e, "TTS synthesis", session_id=session_id)
        finally:
            if tts_path and os.path.exists(tts_path):
                try:
                    os.unlink(tts_path)
                except Exception:
                    pass

        # Deliver audio back through the pipeline/transport if available
        if audio_bytes and self.pipeline is not None:
            try:
                if hasattr(self.pipeline, "send_audio"):
                    self.pipeline.send_audio(session_id, audio_bytes)
                elif hasattr(self.pipeline, "play_audio"):
                    self.pipeline.play_audio(session_id, audio_bytes)
                else:
                    logger.warning("pipecat_pipeline has no send_audio/play_audio method")
            except Exception as e:
                handle_error(e, "sending audio via pipeline", session_id=session_id)

        # Track speaking state for barge-in support (protected)
        with self._lock:
            self.is_speaking[session_id] = bool(audio_bytes)
            self.last_assistant_response[session_id] = response
            if audio_bytes:
                self.last_response_audio_by_session[session_id] = audio_bytes
            # Discard any audio captured during synthesis; playback gating is now active.
            self.audio_buffers.pop(session_id, None)
            self.last_audio_time.pop(session_id, None)
            self.speech_active[session_id] = False

        # For callers that don't have a pipeline, they can also inspect the last response
        # (simple attribute for now)
        self.last_response_text = response
        self.last_response_audio = audio_bytes

    def _send_to_hermes(self, message: str, history: Optional[list] = None) -> str:
        """
        Send a message to the Hermes agent using the injected client.

        Supports a few common client shapes:
          - client.send_message(text, history=...) or client.send(text)
          - client.process(text) or callable client(text)

        Includes basic retry (up to 2 attempts total) with short backoff
        for transient failures. This addresses the TODO item for retries on Hermes.

        Falls back to a safe echo if no usable client is provided.
        """
        if self.hermes is None:
            logger.warning("No hermes_client provided — using echo fallback")
            return f"(no agent) {message}"

        max_attempts = 2
        last_exc: Optional[Exception] = None
        messages = history or [{"role": "user", "content": message}]

        for attempt in range(1, max_attempts + 1):
            try:
                if hasattr(self.hermes, "send_message"):
                    try:
                        return self.hermes.send_message(message, history=messages)
                    except TypeError:
                        return self.hermes.send_message(message)
                elif hasattr(self.hermes, "send"):
                    return self.hermes.send(message)
                elif hasattr(self.hermes, "process"):
                    return self.hermes.process(message)
                elif callable(self.hermes):
                    return self.hermes(message)
                else:
                    # Last resort: try attribute or method that might exist
                    if hasattr(self.hermes, "respond"):
                        return self.hermes.respond(message)
                    return f"(unrecognized client) {message}"
            except Exception as e:
                last_exc = e
                handle_error(e, f"calling Hermes client (attempt {attempt}/{max_attempts})")
                if attempt < max_attempts:
                    time.sleep(0.4 * attempt)  # light backoff: 0.4s then ~0.8s
                continue

        # All attempts failed
        return get_user_friendly_message(last_exc or Exception("unknown"), "contacting the agent")


# Example usage (to be expanded)
def main():
    # These would be real clients/pipelines
    hermes_client = None
    pipecat_pipeline = None

    skill = PipecatHermesSkill(hermes_client, pipecat_pipeline)
    print("Pipecat Hermes Skill initialized.")
    print("Config loaded:", skill.config.hermes.endpoint)
    print("SessionManager ready.")


if __name__ == "__main__":
    main()