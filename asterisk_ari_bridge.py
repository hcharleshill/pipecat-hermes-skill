#!/usr/bin/env python3
"""
Asterisk ARI + External Media bridge for the Pipecat Hermes Skill.

This script connects a SIP call (via Stasis app "hermes") to the
PipecatHermesSkill for full live conversation (STT -> Hermes agent -> TTS).

Usage (after configuring Asterisk):
  source .venv/bin/activate
  python asterisk_ari_bridge.py \
      --ari-url http://localhost:8088 \
      --ari-user asterisk \
      --ari-pass supersecret123 \
      --rtp-host 10.1.1.106 \
      --rtp-port 16000

Then from Linphone dial 101 (or the number you map to Stasis(hermes)).

See asterisk-config/hermes.conf for the standard reusable dialplan configlet.
100 = Echo() media test, 101 = live Stasis(hermes) skill.

The bridge:
- Uses ARI to answer calls and set up ExternalMedia (ulaw).
- Receives RTP from Asterisk, decodes ulaw -> PCM16, upsamples 8k->16k (simple repeat).
- Feeds chunks to skill.handle_incoming_audio().
- Periodically calls check_for_end_of_turn() which triggers Hermes + TTS when the turn ends.
- Plays short verbal acks + the final TTS response (and bleep fillers if slow) back as RTP.
- Supports basic barge-in via energy detection in the skill.

Requirements (in the venv):
  pip install ari requests

The joe voice (en_US-joe-medium) is used by default for all TTS (as set in src/tts.py).
"""

import argparse
import logging
import math
import os
import queue
import random
import socket
import struct
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

import ari
import requests

# Make sure we can import the local skill
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.pipecat_hermes_skill import PipecatHermesSkill
from src.config import config as skill_config
from src import media as media_module
from src import stt as stt_module
from src import telemetry
from src import tts as tts_module
from src.thinking_verbs import long_wait_phrase_pool, spinner_verb_phrases

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("asterisk_ari_bridge")

# --- Minimal RTP helpers (for ExternalMedia ulaw audio) ---
RTP_VERSION = 2
RTP_PAYLOAD_ULAW = 0   # PCMU
RTP_HEADER_SIZE = 12
RTP_CHUNK_ULAW = media_module.RTP_CHUNK_ULAW   # 20ms @ 8kHz (matches Asterisk ptime:20)
RTP_FRAME_SECONDS = 0.02
BLEEP_START_DELAY = 0.8   # seconds after ack before thinking ticks begin
RTP_QUEUE_TARGET_FRAMES = 12   # ~240ms buffer — small enough to avoid long bleep tails
BLEEP_CHUNK_SECONDS = 0.25     # slice size when looping the cached 10s bleep clip
RTP_TTS_PREFETCH_SECONDS = 2.0  # synthesize long-wait phrases ahead of play time


def _boost_thread_priority(label: str = "rtp") -> None:
    """Best-effort: favor the RTP pacer over STT/TTS workers."""
    try:
        os.nice(-5)
    except OSError:
        pass
    if sys.platform == "linux":
        try:
            import ctypes
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            SCHED_FIFO = 1
            class _SchedParam(ctypes.Structure):
                _fields_ = [("sched_priority", ctypes.c_int)]
            param = _SchedParam(50)
            libc.sched_setscheduler(0, SCHED_FIFO, ctypes.byref(param))
        except Exception:
            pass
    logger.debug(f"Thread priority boosted ({label})")


def _lower_thread_priority(label: str = "worker") -> None:
    try:
        os.nice(10)
    except OSError:
        pass
    logger.debug(f"Thread priority lowered ({label})")


def _configure_socket_priority(sock: socket.socket) -> None:
    """Linux: prefer this UDP socket on the transmit queue."""
    if sys.platform != "linux":
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_PRIORITY, 6)
    except OSError:
        pass


def _pad_ulaw_frame(chunk: bytes) -> bytes:
    return media_module.pad_ulaw_frame(chunk, RTP_CHUNK_ULAW)


class RtpPlaybackPacer:
    """
    Dedicated real-time RTP sender. All outbound audio is paced on one thread
    with monotonic 20ms deadlines so Piper/TTS/STT work cannot chop playback.
    """

    def __init__(self, rtp: "SimpleRtpSession"):
        self._rtp = rtp
        self._q: queue.Queue = queue.Queue(maxsize=1024)
        self._stop = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._pending = 0
        self._pending_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._frames_sent = 0
        self._underruns = 0
        self._resyncs = 0
        self._high_watermark = 0
        self._last_resync_log = 0.0
        self._thread = threading.Thread(
            target=self._run, name="rtp-pacer", daemon=True
        )
        self._thread.start()

    def queue_depth(self) -> int:
        return self._q.qsize()

    def pending_frames(self) -> int:
        with self._pending_lock:
            return self._pending

    def stats(self) -> dict:
        with self._pending_lock:
            pending = self._pending
        with self._stats_lock:
            return {
                "queue_depth": self._q.qsize(),
                "pending_frames": pending,
                "frames_sent": self._frames_sent,
                "underruns": self._underruns,
                "resyncs": self._resyncs,
                "high_watermark": self._high_watermark,
            }

    def flush(self) -> None:
        """Drop queued audio immediately (barge-in / cancel)."""
        with self._pending_lock:
            self._pending = 0
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        self._idle.set()

    def enqueue(
        self,
        ulaw: bytes,
        marker: bool = False,
        session_id: Optional[str] = None,
        label: str = "",
    ) -> int:
        """Queue ulaw audio; returns number of 20ms frames queued."""
        if not ulaw:
            return 0
        frame_count = (len(ulaw) + RTP_CHUNK_ULAW - 1) // RTP_CHUNK_ULAW
        # Reserve pending count before queueing so the pacer never sees an empty
        # queue with _pending still at zero (that caused 50ms dropouts / chops).
        with self._pending_lock:
            self._pending += frame_count
            self._idle.clear()
        frames = 0
        first = True
        for i in range(0, len(ulaw), RTP_CHUNK_ULAW):
            chunk = _pad_ulaw_frame(ulaw[i:i + RTP_CHUNK_ULAW])
            self._q.put((chunk, marker and first, session_id, label))
            frames += 1
            first = False
            depth = self._q.qsize()
            with self._stats_lock:
                self._high_watermark = max(self._high_watermark, depth)
        return frames

    def play_blocking(
        self,
        ulaw: bytes,
        marker: bool = False,
        should_stop: Optional[Callable[[], bool]] = None,
        session_id: Optional[str] = None,
        label: str = "",
    ) -> bool:
        """Queue audio and block until every frame is sent (or flush/cancel)."""
        frames = self.enqueue(
            ulaw,
            marker=marker,
            session_id=session_id,
            label=label,
        )
        if frames == 0:
            return True
        while True:
            if should_stop and should_stop():
                self.flush()
                return False
            with self._pending_lock:
                if self._pending <= 0:
                    return True
            time.sleep(0.005)

    def wait_until_idle(self, timeout: float = 30.0) -> None:
        self._idle.wait(timeout=timeout)

    def close(self) -> None:
        self._stop.set()
        self._q.put(None)
        self._thread.join(timeout=2.0)

    def _record_resync(self, late_ms: int, session_id: Optional[str], label: str) -> None:
        with self._stats_lock:
            self._underruns += 1
            self._resyncs += 1
            now = time.monotonic()
            should_log = now - self._last_resync_log >= 1.0
            if should_log:
                self._last_resync_log = now
        if should_log:
            telemetry.log_event(
                "rtp.underrun",
                session_id=session_id,
                label=label,
                late_ms=late_ms,
                queue_depth=self.queue_depth(),
                pending_frames=self.pending_frames(),
            )

    def _run(self) -> None:
        _boost_thread_priority("rtp-pacer")
        next_deadline: Optional[float] = None
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=0.05)
            except queue.Empty:
                with self._pending_lock:
                    still_pending = self._pending > 0
                    if not still_pending:
                        next_deadline = None
                        self._idle.set()
                if still_pending:
                    # Producer is still enqueueing — keep the pacing clock alive.
                    time.sleep(0.002)
                continue

            if item is None:
                break

            chunk, marker, item_session_id, item_label = item
            now = time.monotonic()
            if next_deadline is None:
                next_deadline = now
            elif now < next_deadline:
                time.sleep(next_deadline - now)
            elif now - next_deadline > 0.06:
                # Fell behind — resync rather than burst (bursting sounds choppy).
                self._record_resync(
                    int(round((now - next_deadline) * 1000)),
                    item_session_id,
                    item_label,
                )
                next_deadline = now

            if self._rtp.send_ulaw(chunk, marker=marker):
                with self._stats_lock:
                    self._frames_sent += 1
            next_deadline += RTP_FRAME_SECONDS

            with self._pending_lock:
                self._pending = max(0, self._pending - 1)
                if self._pending <= 0 and self._q.empty():
                    self._idle.set()


class SimpleRtpSession:
    """Very small RTP sender/receiver for ulaw audio over ExternalMedia."""

    def __init__(self, local_port: int, remote_host: Optional[str] = None, remote_port: Optional[int] = None):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        _configure_socket_priority(self.sock)
        self.sock.bind(("0.0.0.0", local_port))
        self.sock.settimeout(0.1)
        self.local_port = local_port

        self.remote_addr: Optional[tuple[str, int]] = None
        if remote_host and remote_port:
            self.remote_addr = (remote_host, remote_port)

        self.ssrc = random.randint(0, 0xffffffff)
        self.seq = random.randint(0, 0xffff)
        self.timestamp = random.randint(0, 0xffffffff)
        self.sample_rate = 8000  # for ulaw external media

        self._lock = threading.Lock()

    def reset_peer(self):
        """Clear learned RTP peer so the next call can re-learn Asterisk's address."""
        with self._lock:
            self.remote_addr = None
            self.seq = random.randint(0, 0xffff)
            self.timestamp = random.randint(0, 0xffffffff)

    def set_peer(self, host: str, port: int):
        """Set the Asterisk UnicastRTP return address from ARI channel variables."""
        with self._lock:
            self.remote_addr = (host, int(port))
            self.seq = random.randint(0, 0xffff)
            self.timestamp = random.randint(0, 0xffffffff)
        logger.info(f"RTP peer set from ARI: {self.remote_addr}")

    def receive_rtp(self, timeout: float = 0.05) -> Optional[bytes]:
        """Receive one RTP payload (ulaw bytes) or None."""
        try:
            data, addr = self.sock.recvfrom(4096)
            if len(data) < RTP_HEADER_SIZE:
                return None

            # Basic RTP header parse (we only care about payload for now)
            version = (data[0] >> 6) & 0x03
            if version != RTP_VERSION:
                return None

            payload_type = data[1] & 0x7f
            if payload_type != RTP_PAYLOAD_ULAW:
                # We only support ulaw for this bridge
                return None

            # Update peer if we don't have one yet (Asterisk tells us where to send return audio)
            if self.remote_addr is None:
                self.remote_addr = addr
                logger.info(f"RTP peer learned: {self.remote_addr}")

            payload = data[RTP_HEADER_SIZE:]
            return payload
        except socket.timeout:
            return None
        except Exception as e:
            logger.debug(f"RTP receive error: {e}")
            return None

    def send_ulaw(self, ulaw_payload: bytes, marker: bool = False) -> bool:
        """Send a chunk of ulaw as RTP to the learned peer. Returns True if sent."""
        if not ulaw_payload or self.remote_addr is None:
            return False

        with self._lock:
            header = struct.pack(
                "!BBHII",
                (RTP_VERSION << 6) | 0,   # version + no padding/ext
                (RTP_PAYLOAD_ULAW | (0x80 if marker else 0)),
                self.seq,
                self.timestamp,
                self.ssrc,
            )
            packet = header + ulaw_payload
            try:
                self.sock.sendto(packet, self.remote_addr)
            except Exception as e:
                logger.warning(f"RTP send error to {self.remote_addr}: {e}")
                return False

            self.seq = (self.seq + 1) & 0xffff
            self.timestamp += len(ulaw_payload)   # 1 sample per byte in ulaw
            return True

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


class AsteriskHermesBridge:
    def __init__(self, ari_url: str, ari_user: str, ari_pass: str,
                 rtp_host: str, rtp_port: int):
        self.ari_url = ari_url
        self.ari_user = ari_user
        self.ari_pass = ari_pass
        self.rtp_host = rtp_host
        self.rtp_port = rtp_port

        # The skill (joe voice is the default in tts.py)
        # We pass no pipeline because we handle audio I/O ourselves.
        # For hermes_client we create a simple HTTP one from config.
        self.hermes_client = self._make_hermes_client()
        self.skill = PipecatHermesSkill(
            hermes_client=self.hermes_client,
            pipecat_pipeline=None,   # we send audio directly via RTP
        )

        self.client: Optional[ari.Client] = None
        self.active_sessions: dict[str, dict] = {}  # session_id -> state
        self._pending_setups: dict[str, dict] = {}  # session_id -> partial bridge setup
        self._sessions_lock = threading.Lock()

        # Bind RTP once at startup so duplicate bridge processes fail immediately.
        try:
            self.rtp = SimpleRtpSession(self.rtp_port)
        except OSError as e:
            if e.errno == 98:
                raise SystemExit(
                    f"UDP port {self.rtp_port} is already in use. "
                    "Only one bridge instance can run at a time. "
                    f"Stop the other process: pgrep -af asterisk_ari_bridge"
                ) from e
            raise

        self.playback = RtpPlaybackPacer(self.rtp)
        self._tts_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tts-prefetch")
        self._bleep_ulaw_cache: bytes = b""
        self._bleep_ulaw_offset = 0
        self._watchdog_stop = threading.Event()
        threading.Thread(
            target=self._session_watchdog,
            daemon=True,
            name="session-watchdog",
        ).start()

        logger.info(f"RTP listening on UDP port {self.rtp_port}")
        logger.info("RTP playback pacer started (high-priority outbound audio)")
        logger.info("PipecatHermesSkill ready (using joe voice by default)")
        self._start_tts_cache_warmup()
        self._start_stt_model_warmup()

    def _make_hermes_client(self):
        """Create a minimal client compatible with the skill's _send_to_hermes expectations."""
        hermes_cfg = skill_config.hermes
        endpoint = getattr(hermes_cfg, "endpoint", "http://localhost:8080")
        backend = getattr(hermes_cfg, "backend", "hermes")
        model = getattr(hermes_cfg, "model", "hermes-agent")
        api_key = getattr(hermes_cfg, "api_key", "")
        timeout = getattr(hermes_cfg, "timeout_seconds", 120)

        class _Client:
            def send_message(self, text: str, history=None) -> str:
                try:
                    messages = history or [{"role": "user", "content": text}]
                    if backend == "openai":
                        return self._send_openai(messages)
                    if backend == "ollama":
                        return self._send_ollama(messages)
                    return self._send_hermes_http(text)
                except Exception as e:
                    logger.warning(f"Agent client error: {e}")
                    return "Sorry, I couldn't reach the agent right now."

            def _send_openai(self, messages) -> str:
                url = f"{endpoint.rstrip('/')}/v1/chat/completions"
                headers = {"Content-Type": "application/json"}
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"
                logger.info(
                    f"Calling Hermes API server model={model} "
                    f"({len(messages)} messages in context) ..."
                )
                r = requests.post(
                    url,
                    headers=headers,
                    json={
                        "model": model,
                        "messages": messages,
                        "stream": False,
                    },
                    timeout=timeout,
                )
                if not r.ok:
                    logger.warning(f"Hermes API {r.status_code}: {r.text[:200]}")
                    return "Sorry, Hermes did not respond."
                data = r.json() if r.content else {}
                content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
                return str(content).strip() or "I don't have a response right now."

            def _send_ollama(self, messages) -> str:
                url = f"{endpoint.rstrip('/')}/api/chat"
                logger.info(f"Calling Ollama model={model} ({len(messages)} messages) ...")
                r = requests.post(
                    url,
                    json={
                        "model": model,
                        "messages": messages,
                        "stream": False,
                    },
                    timeout=timeout,
                )
                if not r.ok:
                    logger.warning(f"Ollama {r.status_code}: {r.text[:200]}")
                    return "Sorry, the language model did not respond."
                data = r.json() if r.content else {}
                content = (data.get("message") or {}).get("content", "")
                return str(content).strip() or "I don't have a response right now."

            def _send_hermes_http(self, text: str) -> str:
                for path in ("/message", "/chat", "/v1/message", ""):
                    url = f"{endpoint.rstrip('/')}{path}"
                    try:
                        r = requests.post(
                            url,
                            json={"message": text, "text": text},
                            timeout=timeout,
                        )
                        if r.ok:
                            data = r.json() if r.content else {}
                            if isinstance(data, dict):
                                for key in ("response", "text", "message", "content"):
                                    if key in data and data[key]:
                                        return str(data[key]).strip()
                            return str(data).strip() if data else text
                    except Exception:
                        continue
                logger.warning(
                    f"No agent response from {endpoint} "
                    "(expected Hermes /message or set hermes.backend=ollama)"
                )
                return "Sorry, the agent service is not available."

        return _Client()

    def _start_tts_cache_warmup(self) -> None:
        """Pre-synthesize fixed phrases and the thinking-bleep loop in the background."""
        phrases = [self.skill.call_greeting_text, *self.skill.short_verbal_acks]
        phrases.extend(spinner_verb_phrases())
        phrases.extend(long_wait_phrase_pool(120.0, 60.0))

        def _warm():
            _lower_thread_priority("tts-warmup")
            try:
                count = tts_module.warm_cache(phrases)
                logger.info(
                    f"TTS phrase cache warmed ({count} new, "
                    f"{tts_module.cache_size()} total entries)"
                )
            except Exception as e:
                logger.warning(f"TTS cache warmup failed (lazy cache still works): {e}")
            try:
                pcm = self.skill.warm_listening_bleeps_cache()
                self._bleep_ulaw_cache = media_module.audio_bytes_to_ulaw(pcm, pcm_sample_rate=16000)
                self._bleep_ulaw_offset = 0
                logger.info(
                    f"Thinking bleep loop ready for RTP "
                    f"({len(self._bleep_ulaw_cache)} ulaw bytes, "
                    f"~{len(self._bleep_ulaw_cache) / 8000:.1f}s)"
                )
            except Exception as e:
                logger.warning(f"Bleep cache warmup failed (will render on demand): {e}")

        threading.Thread(target=_warm, daemon=True, name="tts-cache-warmup").start()

    def _start_stt_model_warmup(self) -> None:
        """Load Faster-Whisper in the background so the first turn is not cold."""
        def _warm():
            _lower_thread_priority("stt-warmup")
            try:
                stt_module.get_model()
                logger.info("STT model warmed (first user turn skips model-load latency)")
            except Exception as e:
                logger.warning(f"STT warmup failed (lazy load will retry on first turn): {e}")

        threading.Thread(target=_warm, daemon=True, name="stt-model-warmup").start()

    def _session_watchdog(self) -> None:
        """
        Reap sessions if ARI hangup events are missed or workers run too long.

        Checks every 10s for: no inbound RTP after the call should be alive,
        turn pipeline stuck, or absolute max call duration exceeded.
        """
        _lower_thread_priority("session-watchdog")
        max_call_seconds = 3600
        max_rtp_idle_seconds = 90
        max_turn_seconds = 300
        while not self._watchdog_stop.wait(timeout=10.0):
            now = time.time()
            with self._sessions_lock:
                snapshot = [
                    (sid, dict(meta))
                    for sid, meta in self.active_sessions.items()
                    if meta.get("running")
                ]
            for session_id, sess in snapshot:
                started = sess.get("started_at") or now
                last_rtp = sess.get("last_rtp_at") or started
                turn_started = sess.get("turn_started_at")
                reason = None
                if now - started > max_call_seconds:
                    reason = f"max call duration ({max_call_seconds}s)"
                elif now - last_rtp > max_rtp_idle_seconds:
                    reason = f"no inbound RTP for {max_rtp_idle_seconds}s"
                elif (
                    sess.get("turn_worker_running")
                    and turn_started
                    and now - turn_started > max_turn_seconds
                ):
                    reason = f"turn worker exceeded {max_turn_seconds}s"
                if reason:
                    logger.warning(
                        f"[{session_id}] Watchdog forcing stop: {reason}"
                    )
                    self._stop_session(session_id)

    def _enqueue_cached_bleeps(
        self,
        session_id: Optional[str] = None,
        marker: bool = False,
    ) -> bool:
        """Enqueue the next slice from the cached 10s thinking-bleep loop."""
        cache = self._bleep_ulaw_cache
        if not cache:
            pcm = self.skill.warm_listening_bleeps_cache()
            cache = media_module.audio_bytes_to_ulaw(pcm, pcm_sample_rate=16000)
            self._bleep_ulaw_cache = cache
            self._bleep_ulaw_offset = 0
        if not cache:
            return False

        chunk_samples = max(RTP_CHUNK_ULAW, int(BLEEP_CHUNK_SECONDS * 8000))
        chunk_len = (
            (chunk_samples + RTP_CHUNK_ULAW - 1) // RTP_CHUNK_ULAW
        ) * RTP_CHUNK_ULAW

        start = self._bleep_ulaw_offset
        end = start + chunk_len
        if end <= len(cache):
            chunk = cache[start:end]
        else:
            wrap = end % len(cache)
            chunk = cache[start:] + cache[:wrap]

        self._bleep_ulaw_offset = end % len(cache)
        self.playback.enqueue(
            chunk,
            marker=marker,
            session_id=session_id,
            label="thinking-bleep",
        )
        return True

    def start(self):
        logger.info(f"Connecting to ARI at {self.ari_url} ...")
        self.client = ari.connect(self.ari_url, self.ari_user, self.ari_pass)

        # Subscribe to our Stasis app
        self.client.on_channel_event("StasisStart", self.on_stasis_start)
        self.client.on_channel_event("StasisEnd", self.on_stasis_end)

        logger.info("Connected to ARI. Waiting for calls on Stasis app 'hermes'...")
        logger.info("Dial 101 from Linphone to reach the live skill.")
        logger.info(f"RTP will be sent to {self.rtp_host}:{self.rtp_port} (set this in externalMedia)")

        # Run the ARI event loop (blocking)
        self.client.run(apps="hermes")

    def on_stasis_start(self, event, channel):
        """A call entered our Stasis app."""
        channel_id = channel.json.get("id")
        if not channel_id:
            return

        if channel_id.startswith("media-"):
            # ExternalMedia channel is ready — finish bridge + RTP setup.
            session_id = channel_id[len("media-"):]
            try:
                channel.answer()
            except Exception:
                pass
            self._finalize_media_setup(session_id, channel_id)
            return

        session_id = channel_id
        logger.info(f"Call {session_id} entered Stasis 'hermes'")

        # Only one RTP consumer at a time; tear down any leaked prior call state.
        self._stop_other_sessions(keep=session_id)

        try:
            channel.answer()

            bridge = self.client.bridges.create(type="mixing")
            bridge_id = bridge.json.get("id")
            logger.info(f"[{session_id}] Created mixing bridge {bridge_id}")

            # Add caller first; ExternalMedia joins on its own StasisStart.
            bridge.addChannel(channel=channel_id)
            logger.info(f"[{session_id}] Added caller {channel_id} to bridge {bridge_id}")

            media_channel_id = f"media-{channel_id}"
            self._pending_setups[session_id] = {
                "bridge_id": bridge_id,
                "media_channel_id": media_channel_id,
                "caller_channel_id": channel_id,
            }

            self.client.channels.externalMedia(
                channelId=media_channel_id,
                app="hermes",
                externalMediaOptions={
                    "data": f"{self.rtp_host}:{self.rtp_port}",
                    "format": "ulaw",
                },
            )
            logger.info(f"[{session_id}] ExternalMedia creation requested ({media_channel_id})")

        except Exception as e:
            logger.exception(f"Failed to set up media for {session_id}: {e}")
            self._pending_setups.pop(session_id, None)
            try:
                channel.hangup()
            except Exception:
                pass

    def _finalize_media_setup(self, session_id: str, media_channel_id: str):
        """Add ExternalMedia to the mixing bridge and start RTP once the channel is in Stasis."""
        pending = self._pending_setups.pop(session_id, None)
        if not pending:
            logger.warning(f"[{session_id}] ExternalMedia ready but no pending setup")
            return

        bridge_id = pending.get("bridge_id")
        try:
            self.client.bridges.addChannel(bridgeId=bridge_id, channel=media_channel_id)
            logger.info(
                f"[{session_id}] Added {media_channel_id} to mixing bridge {bridge_id}"
            )
        except Exception as e:
            logger.exception(f"[{session_id}] Failed to add ExternalMedia to bridge: {e}")
            self._stop_session(session_id)
            return

        self._configure_rtp_peer(media_channel_id)
        self.skill.mark_session_start(session_id)
        self._start_media_session(
            session_id,
            media_channel_id,
            bridge_id=bridge_id,
            caller_channel_id=pending.get("caller_channel_id"),
        )

    def _configure_rtp_peer(self, media_channel_id: str):
        """Read UNICASTRTP_* from ARI so return RTP targets Asterisk's socket."""
        if not self.client:
            return
        try:
            host = self.client.channels.getChannelVar(
                channelId=media_channel_id,
                variable="UNICASTRTP_LOCAL_ADDRESS",
            )
            port = self.client.channels.getChannelVar(
                channelId=media_channel_id,
                variable="UNICASTRTP_LOCAL_PORT",
            )
            if host and port:
                self.rtp.set_peer(host, int(port))
            else:
                logger.warning(
                    f"UNICASTRTP vars missing for {media_channel_id}; "
                    "will learn peer from first inbound RTP packet"
                )
        except Exception as e:
            logger.warning(f"Could not read UNICASTRTP vars for {media_channel_id}: {e}")

    def _session_id_from_channel(self, channel_id: str) -> Optional[str]:
        if not channel_id:
            return None
        if channel_id.startswith("media-"):
            return channel_id[len("media-") :]
        return channel_id

    def _is_session_active(self, session_id: str) -> bool:
        with self._sessions_lock:
            sess = self.active_sessions.get(session_id)
            return bool(sess and sess.get("running"))

    def on_stasis_end(self, event, channel):
        channel_id = channel.json.get("id")
        session_id = self._session_id_from_channel(channel_id)
        if not session_id:
            return
        if session_id in self.active_sessions or session_id in self._pending_setups:
            logger.info(f"Call {session_id} ended (channel {channel_id})")
            self._stop_session(session_id)

    def _stop_other_sessions(self, keep: Optional[str] = None):
        with self._sessions_lock:
            stale = [sid for sid in self.active_sessions if sid != keep]
        for sid in stale:
            logger.warning(f"Stopping stale session {sid} before new call")
            self._stop_session(sid)

    def _stop_session(self, session_id: str):
        pending = self._pending_setups.pop(session_id, None)
        with self._sessions_lock:
            sess = self.active_sessions.pop(session_id, None)

        if sess:
            sess["running"] = False
            sess["playback_cancel"] = True
            self.playback.flush()
            media_channel_id = sess.get("media_channel_id")
            bridge_id = sess.get("bridge_id")
            caller_channel_id = sess.get("caller_channel_id")
        elif pending:
            sess = None
            media_channel_id = pending.get("media_channel_id")
            bridge_id = pending.get("bridge_id")
            caller_channel_id = pending.get("caller_channel_id")
            self.playback.flush()
            logger.info(f"[{session_id}] Cleaning up call that ended during setup")
        else:
            return

        if self.client:
            for cid in (media_channel_id, caller_channel_id):
                if not cid:
                    continue
                try:
                    self.client.channels.hangup(channelId=cid)
                except Exception:
                    pass
        if self.client and bridge_id:
            try:
                self.client.bridges.destroy(bridgeId=bridge_id)
            except Exception:
                pass

        self.rtp.reset_peer()
        self.skill.end_session(session_id)
        logger.info(f"[{session_id}] Session stopped and resources released")

    def _start_media_session(
        self,
        session_id: str,
        media_channel_id: str,
        bridge_id: Optional[str] = None,
        caller_channel_id: Optional[str] = None,
    ):
        """Spawn threads to handle RTP <-> skill for one call."""
        # Keep the UNICASTRTP peer set in _configure_rtp_peer — do not reset here.

        now = time.time()
        sess = {
            "media_channel_id": media_channel_id,
            "bridge_id": bridge_id,
            "caller_channel_id": caller_channel_id,
            "running": True,
            "playback_cancel": False,
            "turn_worker_running": False,
            "started_at": now,
            "last_rtp_at": now,
            "turn_started_at": None,
        }
        with self._sessions_lock:
            self.active_sessions[session_id] = sess

        # Probe must finish before rx_thread starts (single UDP socket consumer).
        self._run_echo_probe(session_id)
        self._play_call_greeting(session_id)

        # Thread that receives RTP from Asterisk and feeds the skill
        def rx_thread():
            _lower_thread_priority("rtp-rx")
            logger.info(f"[{session_id}] RTP receiver started on port {self.rtp_port}")
            while sess.get("running"):
                ulaw = self.rtp.receive_rtp(timeout=0.05)
                if ulaw:
                    sess["last_rtp_at"] = time.time()
                    pcm8 = media_module.ulaw_to_pcm16(ulaw)
                    pcm16 = media_module.upsample_8k_to_16k(pcm8)
                    if self.skill.should_monitor_playback(session_id):
                        if self.skill.monitor_playback_audio(session_id, pcm16):
                            sess["playback_cancel"] = True
                            self.playback.flush()
                            self.skill.on_keyword_interrupt(session_id)
                            continue
                    self.skill.handle_incoming_audio(session_id, pcm16)
                    if self.skill.consume_playback_interrupt(session_id):
                        sess["playback_cancel"] = True
                        self.playback.flush()

                if sess.get("turn_worker_running"):
                    continue

                # Call the turn check frequently. The in-skill guards (speech_active,
                # min buffer, energy-only timing) prevent micro-turns. Calling only on
                # gaps can miss detection if Asterisk sends continuous (low-energy)
                # packets during silence.
                text = self.skill.check_for_end_of_turn(session_id)
                if text:
                    sess["turn_worker_running"] = True
                    sess["turn_started_at"] = time.time()
                    telemetry.log_event(
                        "turn.accepted",
                        session_id=session_id,
                        transcript_chars=len(text),
                        rtp_queue_depth=self.playback.queue_depth(),
                    )
                    threading.Thread(
                        target=self._handle_turn_end,
                        args=(session_id, text),
                        daemon=True,
                    ).start()

        t = threading.Thread(target=rx_thread, daemon=True, name=f"rtp-rx-{session_id}")
        t.start()

    def _run_echo_probe(self, session_id: str):
        """
        Play a short tone and measure how much comes back on the mic.

        High return energy → speakerphone / open mic → strict half-duplex + 'stop' interrupt.
        Low return energy  → likely headphones   → allow energy barge-in.
        """
        sess = self.active_sessions.get(session_id)
        if not sess or not sess.get("running"):
            return
        deadline = time.time() + 2.0
        while sess.get("running") and self.rtp.remote_addr is None and time.time() < deadline:
            time.sleep(0.05)
        if not sess.get("running"):
            return
        if self.rtp.remote_addr is None:
            media_id = sess.get("media_channel_id")
            if media_id:
                self._configure_rtp_peer(media_id)
        if self.rtp.remote_addr is None:
            logger.warning(
                f"[{session_id}] Echo probe skipped (no RTP peer) — defaulting to speakerphone"
            )
            self.skill.set_acoustic_mode(session_id, "speakerphone", echo_rms=0.0)
            return

        # ~400ms probe tone @ 440Hz (8kHz PCM before ulaw)
        tone = bytearray()
        for i in range(3200):
            sample = int(9000 * math.sin(2 * math.pi * 440 * i / 8000))
            tone.extend(struct.pack("<h", sample))
        ulaw = media_module.pcm16_to_ulaw(bytes(tone))

        echo_rms_readings: list[float] = []
        packets = 0
        for i in range(0, len(ulaw), RTP_CHUNK_ULAW):
            if not sess.get("running"):
                return
            chunk = ulaw[i:i + RTP_CHUNK_ULAW]
            if self.rtp.send_ulaw(chunk, marker=(packets == 0)):
                packets += 1
            time.sleep(0.02)
            for _ in range(2):
                inbound = self.rtp.receive_rtp(timeout=0.03)
                if inbound:
                    pcm8 = media_module.ulaw_to_pcm16(inbound)
                    pcm16 = media_module.upsample_8k_to_16k(pcm8)
                    rms = media_module.pcm16_rms(pcm16)
                    if rms > 0:
                        echo_rms_readings.append(rms)

        avg_echo = (
            sum(echo_rms_readings) / len(echo_rms_readings) if echo_rms_readings else 0.0
        )
        threshold = self.skill.echo_probe_rms_threshold
        mode = "speakerphone" if avg_echo >= threshold else "isolated"
        self.skill.set_acoustic_mode(session_id, mode, echo_rms=avg_echo)

        if packets:
            logger.info(f"[{session_id}] Echo probe tone sent ({packets} packets)")

    def _play_call_greeting(self, session_id: str) -> None:
        """Play the opening Hermes greeting before listening for the first user turn."""
        sess = self.active_sessions.get(session_id)
        if not sess or not sess.get("running"):
            return
        if self.rtp.remote_addr is None:
            logger.warning(f"[{session_id}] Skipping call greeting (no RTP peer)")
            return

        greeting = self.skill.get_call_greeting_audio()
        if not greeting:
            return

        logger.info(f"[{session_id}] Playing call greeting")
        self.skill.begin_agent_playback(session_id)
        try:
            self._play_audio_bytes(
                self.rtp,
                greeting,
                session_id,
                sess,
                label="greeting",
            )
        finally:
            self.skill.end_agent_playback(session_id)

    def _handle_turn_end(self, session_id: str, text: str):
        """
        Turn UX pipeline (no dead air):
          1. Verbal ack immediately ("Gotcha.", etc.)
          2. Hermes + TTS in background
          3. Thinking bleeps while waiting (after ~800ms)
          4. Final spoken response
        """
        try:
            logger.info(f"[{session_id}] Turn ended. User said: {text!r}")

            sess = self.active_sessions.get(session_id)
            if not sess or not sess.get("running"):
                return
            rtp: SimpleRtpSession = self.rtp

            if rtp.remote_addr is None:
                logger.warning(
                    f"[{session_id}] No RTP peer yet — playback may be silent until "
                    "Asterisk sends the first inbound packet"
                )

            sess["playback_cancel"] = False
            hermes_done = threading.Event()
            wait_start = time.monotonic()

            def _hermes_worker():
                _lower_thread_priority("hermes")
                try:
                    if not self._is_session_active(session_id):
                        return
                    with self.skill._lock:
                        self.skill.last_response_audio_by_session.pop(session_id, None)
                        self.skill.last_response_text_by_session.pop(session_id, None)
                    self.skill.route_message(
                        session_id,
                        text,
                        alive_check=lambda: self._is_session_active(session_id),
                        synthesize_audio=False,
                    )
                except Exception as e:
                    logger.exception(f"[{session_id}] Hermes worker failed: {e}")
                finally:
                    hermes_done.set()

            threading.Thread(
                target=_hermes_worker,
                daemon=True,
                name=f"hermes-{session_id}",
            ).start()

            self.skill.begin_agent_playback(session_id)
            try:
                ack = self.skill.get_turn_acknowledgement(session_id)
                if ack and not sess.get("playback_cancel"):
                    self._play_audio_bytes(rtp, ack, session_id, sess, label="ack")

                if not sess.get("playback_cancel"):
                    self._play_thinking_bleeps_until(
                        rtp,
                        session_id,
                        sess,
                        hermes_done,
                        wait_start=wait_start,
                        start_delay=BLEEP_START_DELAY,
                    )

                with self.skill._lock:
                    response_text = self.skill.last_response_text_by_session.get(session_id)
                    full_wav = self.skill.last_response_audio_by_session.get(session_id)
                if response_text and not sess.get("playback_cancel"):
                    self._play_tts_text_streaming(
                        rtp,
                        response_text,
                        session_id,
                        sess,
                        label="response",
                    )
                elif full_wav and not sess.get("playback_cancel"):
                    self._play_audio_bytes(rtp, full_wav, session_id, sess, label="response")
                elif not sess.get("playback_cancel"):
                    logger.warning(f"[{session_id}] No response audio, synthesizing fallback")
                    try:
                        fallback = tts_module.synthesize_to_wav_bytes(
                            text or "Sorry, I didn't catch that.",
                            use_cache=False,
                        )
                        self._play_audio_bytes(rtp, fallback, session_id, sess, label="fallback")
                    except Exception as e:
                        logger.error(f"Fallback TTS failed: {e}")
            finally:
                self.skill.end_agent_playback(session_id)
        finally:
            sess = self.active_sessions.get(session_id)
            if sess:
                sess["turn_worker_running"] = False
                sess["turn_started_at"] = None
            if self._is_session_active(session_id):
                self.skill.finish_agent_turn(session_id)

    def _play_thinking_bleeps_until(
        self,
        rtp: SimpleRtpSession,
        session_id: str,
        sess: dict,
        done: threading.Event,
        wait_start: float,
        start_delay: float = BLEEP_START_DELAY,
    ):
        """Stream procedural tones while Hermes works; prefetch spoken long-wait TTS."""
        if start_delay > 0:
            if done.wait(timeout=start_delay):
                return

        first_delay = getattr(self.skill, "long_wait_first_seconds", 7.0)
        interval_range = getattr(self.skill, "long_wait_interval_jitter", (7.0, 8.0))
        next_long_wait_at = wait_start + first_delay
        last_long_wait_phrase = None
        last_long_wait_audio = None
        long_wait_repeat_count = 0
        pending_long_wait = None
        bleep_marker = True
        stop = lambda: sess.get("playback_cancel") or not sess.get("running")

        def _next_spinner_delay() -> float:
            lo, hi = interval_range
            return random.uniform(lo, hi)

        while sess.get("running") and not sess.get("playback_cancel"):
            if done.is_set():
                break

            now = time.monotonic()
            elapsed = now - wait_start

            # Prefetch long-wait TTS before it is due (Piper must not block the pacer).
            if (
                pending_long_wait is None
                and now >= next_long_wait_at - RTP_TTS_PREFETCH_SECONDS
            ):
                pending_long_wait = self._tts_executor.submit(
                    self.skill.get_long_wait_acknowledgement,
                    session_id,
                    last_long_wait_phrase,
                    last_long_wait_audio,
                    long_wait_repeat_count,
                    elapsed,
                )

            if now >= next_long_wait_at:
                played = False
                if pending_long_wait is not None and pending_long_wait.done():
                    try:
                        result = pending_long_wait.result()
                    except Exception as e:
                        logger.warning(f"[{session_id}] Long-wait TTS failed: {e}")
                        result = None
                    pending_long_wait = None
                    if result and not stop():
                        phrase, long_wait_audio = result
                        if phrase == last_long_wait_phrase:
                            long_wait_repeat_count += 1
                        else:
                            long_wait_repeat_count = 0
                        last_long_wait_phrase = phrase
                        last_long_wait_audio = long_wait_audio
                        self._play_audio_bytes(
                            rtp,
                            long_wait_audio,
                            session_id,
                            sess,
                            label="long-wait",
                        )
                        played = True
                        bleep_marker = True
                    next_long_wait_at = now + _next_spinner_delay()
                elif pending_long_wait is not None:
                    # TTS still rendering — retry shortly without losing the prefetch.
                    next_long_wait_at = now + 0.25
                else:
                    next_long_wait_at = now + _next_spinner_delay()
                if played and done.is_set():
                    break

            # Keep the pacer fed so STT/TTS jitter does not drain the queue.
            while (
                self.playback.queue_depth() < RTP_QUEUE_TARGET_FRAMES
                and sess.get("running")
                and not sess.get("playback_cancel")
                and not done.is_set()
            ):
                if not self._enqueue_cached_bleeps(
                    session_id=session_id,
                    marker=bleep_marker,
                ):
                    break
                bleep_marker = False

            if done.wait(timeout=0.02):
                break

        self.playback.wait_until_idle(timeout=5.0)

    def _play_audio_bytes(
        self,
        rtp: SimpleRtpSession,
        audio_bytes: bytes,
        session_id: str,
        sess: dict,
        label: str = "",
        pcm_sample_rate: int = 22050,
    ):
        """Convert audio off-thread path, then play via the high-priority RTP pacer."""
        if not audio_bytes or sess.get("playback_cancel"):
            return

        stats_before = self.playback.stats()
        ulaw = b""
        packets_sent = 0
        convert_ms = 0
        playback_ms = 0
        ok = False
        error_type: Optional[str] = None
        try:
            convert_started = telemetry.now()
            ulaw = media_module.audio_bytes_to_ulaw(audio_bytes, pcm_sample_rate=pcm_sample_rate)
            convert_ms = telemetry.elapsed_ms(convert_started)
            if not ulaw:
                logger.warning(f"[{session_id}] No ulaw audio to play for {label}")
                return

            packets_sent = (len(ulaw) + RTP_CHUNK_ULAW - 1) // RTP_CHUNK_ULAW
            playback_started = telemetry.now()
            ok = self.playback.play_blocking(
                ulaw,
                marker=True,
                should_stop=lambda: sess.get("playback_cancel") or not sess.get("running"),
                session_id=session_id,
                label=label,
            )
            playback_ms = telemetry.elapsed_ms(playback_started)
            if ok:
                logger.info(
                    f"[{session_id}] Played {label}: {packets_sent} RTP packets "
                    f"({len(ulaw)} ulaw bytes) -> {rtp.remote_addr}"
                )
            elif sess.get("playback_cancel"):
                logger.debug(f"[{session_id}] Playback cancelled during {label}")
            else:
                logger.warning(
                    f"[{session_id}] Failed to play {label}: no RTP peer "
                    f"(remote_addr={rtp.remote_addr})"
                )
        except Exception as e:
            error_type = type(e).__name__
            logger.exception(f"[{session_id}] Error playing {label} audio: {e}")
        finally:
            stats_after = self.playback.stats()
            fields = {
                "label": label,
                "ok": ok,
                "frames_queued": packets_sent,
                "ulaw_bytes": len(ulaw),
                "convert_ms": convert_ms,
                "playback_ms": playback_ms,
                "queue_depth_before": stats_before.get("queue_depth", 0),
                "queue_depth_after": stats_after.get("queue_depth", 0),
                "queue_high_watermark": stats_after.get("high_watermark", 0),
                "underruns_delta": stats_after.get("underruns", 0)
                - stats_before.get("underruns", 0),
                "resyncs_delta": stats_after.get("resyncs", 0)
                - stats_before.get("resyncs", 0),
                "has_remote_peer": rtp.remote_addr is not None,
            }
            if error_type:
                fields["error_type"] = error_type
            telemetry.log_event("rtp.playback", session_id=session_id, **fields)

    def _play_tts_text_streaming(
        self,
        rtp: SimpleRtpSession,
        text: str,
        session_id: str,
        sess: dict,
        label: str = "tts",
    ) -> bool:
        """Stream Piper PCM chunks through the RTP pacer as they are produced."""
        if not text or sess.get("playback_cancel"):
            return False

        stop = lambda: sess.get("playback_cancel") or not sess.get("running")
        max_queue_depth = max(RTP_QUEUE_TARGET_FRAMES * 6, 48)
        stats_before = self.playback.stats()
        tts_started = telemetry.now()
        first_pcm_ms: Optional[int] = None
        total_generation_ms: Optional[int] = None
        playback_wait_ms = 0
        chunks = 0
        pcm_bytes = 0
        ulaw_bytes = 0
        frames_queued = 0
        max_queue_depth_seen = self.playback.queue_depth()
        backpressure_events = 0
        backpressure_wait_ms = 0
        underrun_events = 0
        started = False
        generation_complete = False
        ok = False
        cancelled = False
        error_type: Optional[str] = None

        try:
            for pcm16, sample_rate in tts_module.iter_synthesize_pcm16_chunks(text):
                if stop():
                    cancelled = True
                    self.playback.flush()
                    return False
                chunks += 1
                pcm_bytes += len(pcm16)
                if first_pcm_ms is None:
                    first_pcm_ms = telemetry.elapsed_ms(tts_started)
                    telemetry.log_event(
                        "tts.first_pcm",
                        session_id=session_id,
                        label=label,
                        elapsed_ms=first_pcm_ms,
                        text_chars=len(text),
                        pcm_bytes=len(pcm16),
                        sample_rate=sample_rate,
                    )

                pending_before = self.playback.pending_frames()
                queue_before = self.playback.queue_depth()
                max_queue_depth_seen = max(max_queue_depth_seen, queue_before)
                if started and pending_before <= 0:
                    underrun_events += 1
                    telemetry.log_event(
                        "rtp.underrun",
                        session_id=session_id,
                        label=label,
                        source="tts-stream",
                        queue_depth=queue_before,
                        pending_frames=pending_before,
                    )

                ulaw = media_module.pcm16_to_ulaw_bytes(
                    pcm16,
                    sample_rate=sample_rate,
                    out_rate=8000,
                )
                if not ulaw:
                    continue
                ulaw_bytes += len(ulaw)

                wait_started: Optional[float] = None
                while self.playback.queue_depth() > max_queue_depth:
                    if stop():
                        cancelled = True
                        self.playback.flush()
                        return False
                    if wait_started is None:
                        wait_started = telemetry.now()
                        backpressure_events += 1
                    time.sleep(0.01)
                if wait_started is not None:
                    waited_ms = telemetry.elapsed_ms(wait_started)
                    backpressure_wait_ms += waited_ms
                    telemetry.log_event(
                        "rtp.backpressure",
                        session_id=session_id,
                        label=label,
                        waited_ms=waited_ms,
                        queue_depth=self.playback.queue_depth(),
                        max_queue_depth=max_queue_depth,
                    )

                frames_queued += self.playback.enqueue(
                    ulaw,
                    marker=not started,
                    session_id=session_id,
                    label=label,
                )
                started = True
                max_queue_depth_seen = max(
                    max_queue_depth_seen,
                    self.playback.queue_depth(),
                )

            generation_complete = True
            total_generation_ms = telemetry.elapsed_ms(tts_started)

            if not started:
                logger.warning(f"[{session_id}] Streaming TTS produced no audio for {label}")
                return False

            timeout = max(5.0, frames_queued * RTP_FRAME_SECONDS + 2.0)
            playback_wait_started = telemetry.now()
            self.playback.wait_until_idle(timeout=timeout)
            playback_wait_ms = telemetry.elapsed_ms(playback_wait_started)
            if stop():
                cancelled = True
                self.playback.flush()
                return False

            logger.info(
                f"[{session_id}] Played streaming {label}: "
                f"{frames_queued} RTP packets -> {rtp.remote_addr}"
            )
            ok = True
            return True
        except Exception as e:
            error_type = type(e).__name__
            logger.exception(f"[{session_id}] Error streaming {label} TTS: {e}")
            return False
        finally:
            stats_after = self.playback.stats()
            fields = {
                "label": label,
                "ok": ok,
                "cancelled": cancelled,
                "generation_complete": generation_complete,
                "first_pcm_ms": first_pcm_ms,
                "total_generation_ms": (
                    total_generation_ms
                    if total_generation_ms is not None
                    else telemetry.elapsed_ms(tts_started)
                ),
                "playback_wait_ms": playback_wait_ms,
                "text_chars": len(text),
                "chunks": chunks,
                "pcm_bytes": pcm_bytes,
                "ulaw_bytes": ulaw_bytes,
                "frames_queued": frames_queued,
                "queue_depth_after": stats_after.get("queue_depth", 0),
                "queue_depth_max_seen": max_queue_depth_seen,
                "queue_high_watermark": stats_after.get("high_watermark", 0),
                "backpressure_events": backpressure_events,
                "backpressure_wait_ms": backpressure_wait_ms,
                "underrun_events": underrun_events,
                "underruns_delta": stats_after.get("underruns", 0)
                - stats_before.get("underruns", 0),
                "resyncs_delta": stats_after.get("resyncs", 0)
                - stats_before.get("resyncs", 0),
                "has_remote_peer": rtp.remote_addr is not None,
            }
            if error_type:
                fields["error_type"] = error_type
            telemetry.log_event("tts.stream", session_id=session_id, **fields)


def main():
    parser = argparse.ArgumentParser(description="Asterisk ARI bridge for PipecatHermesSkill (joe voice)")
    parser.add_argument("--ari-url", default="http://localhost:8088", help="ARI HTTP base URL")
    parser.add_argument("--ari-user", default="asterisk", help="ARI username")
    parser.add_argument("--ari-pass", default="supersecret123", help="ARI password")
    parser.add_argument("--rtp-host", default="10.1.1.106",
                        help="IP address the Asterisk container should send RTP *to* (your host LAN IP)")
    parser.add_argument("--rtp-port", type=int, default=16000, help="UDP port for RTP (must be free on host)")
    args = parser.parse_args()

    bridge = AsteriskHermesBridge(
        ari_url=args.ari_url,
        ari_user=args.ari_user,
        ari_pass=args.ari_pass,
        rtp_host=args.rtp_host,
        rtp_port=args.rtp_port,
    )

    print("\n=== Asterisk ARI Hermes Bridge ===")
    print(f"ARI: {args.ari_url}")
    print(f"RTP receive/send address for Asterisk: {args.rtp_host}:{args.rtp_port}")
    print("Make sure your Asterisk container has ARI enabled on 8088 and extension 101 -> Stasis(hermes)")
    print("See asterisk-config/hermes.conf (the standard reusable dialplan configlet).")
    print("100 = Echo() media test, 101 = live Stasis(hermes) skill.")
    print("Press Ctrl-C to stop.\n")

    try:
        bridge.start()
    except KeyboardInterrupt:
        logger.info("Bridge stopped by user.")


if __name__ == "__main__":
    main()
