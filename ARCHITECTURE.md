# Pipecat Hermes Skill / Asterisk Voice Agent UX Skill - Architecture

## 1. Overview

This project is currently named **Pipecat Hermes Skill**, but its working shape
is broader: an **Asterisk voice-agent UX skill**. It sits between an Asterisk
phone call and a text-first AI agent such as Hermes, Openclaw, Ollama, or an
OpenAI-compatible gateway.

Its job is not just transport plumbing. It owns the voice UX around the agent:

- RTP audio ingress/egress for Asterisk calls.
- Speech-to-text for caller turns.
- Agent request/response routing.
- Text-to-speech playback back to the caller.
- Turn taking, acknowledgements, dead-air handling, and barge-in behavior.
- Session persistence and cleanup.

The current implementation is synchronous/threaded Python with a narrow media
conversion module that can later be replaced by an optional native/Rust backend.

## 2. System Context

```text
SIP phone
  -> Asterisk Stasis / ExternalMedia
  -> asterisk_ari_bridge.py
  -> src.media              (PCM/u-law/resampling/RTP frame helpers)
  -> PipecatHermesSkill     (turn state, session state, agent routing)
  -> src.stt                (Faster-Whisper)
  -> Hermes/Openclaw/etc.   (text agent backend)
  -> src.tts                (Piper)
  -> RTP pacer
  -> Asterisk
  -> SIP phone
```

The bridge currently uses Asterisk ARI + ExternalMedia with PCMU/u-law RTP.
Incoming Asterisk RTP is decoded to PCM16, upsampled to 16 kHz for STT, routed
through the agent, then TTS output is converted back to 8 kHz PCMU/u-law for RTP
playback.

## 3. Main Layers

### Asterisk Bridge

File: `asterisk_ari_bridge.py`

Responsibilities:

- Connect to Asterisk ARI.
- Answer Stasis calls and create ExternalMedia channels.
- Receive inbound RTP packets from Asterisk.
- Feed decoded PCM into `PipecatHermesSkill`.
- Play acknowledgements, thinking bleeps, long-wait cues, and final responses.
- Pace outbound RTP on a dedicated sender thread.
- Run background warmups for STT and fixed TTS phrases.
- Reap stuck sessions with a watchdog.

The bridge is intentionally transport-specific. It knows about ARI, RTP, PCMU,
ports, channels, bridges, and call lifecycle.

### Skill Orchestration

File: `src/pipecat_hermes_skill.py`

Responsibilities:

- Buffer inbound PCM chunks by session.
- Detect speech energy and turn boundaries.
- Transcribe completed caller turns.
- Route text to the injected agent client.
- Maintain session history via `SessionManager`.
- Track transient voice state such as speaking, playback, barge-in, and cooldown.
- Provide reusable call UX audio such as greetings, short acknowledgements, and
  long-wait phrases.

For live Asterisk calls, `route_message(..., synthesize_audio=False)` returns
agent text and lets the bridge stream TTS directly. This keeps the bridge in
control of RTP pacing and avoids waiting for a full WAV response before
playback can begin.

### Media Conversion

File: `src/media.py`

Responsibilities:

- PCMU/u-law to PCM16 conversion.
- PCM16 to PCMU/u-law conversion.
- 8 kHz to 16 kHz upsampling for STT input.
- PCM16 resampling for TTS output to Asterisk's 8 kHz RTP path.
- RMS calculation for echo probe and speech/interrupt logic.
- WAV byte decoding.
- RTP-sized u-law frame padding.

`src.media` first tries to import `src._media_native`. If that optional module
exists, matching functions can be provided by a compiled backend. Otherwise the
pure Python/audioop-based implementation is used.

This is the intended boundary for future Rust/PyO3 acceleration.

### STT

File: `src/stt.py`

Responsibilities:

- Lazy-load a Faster-Whisper model.
- Prefer CUDA/float16 when available.
- Fall back to CPU/int8 when CUDA initialization fails.
- Provide file-based transcription for compatibility.
- Provide `transcribe_pcm16()` for the live call path.

The live path now avoids writing a temporary WAV file. Captured PCM16 is
converted directly to a normalized float32 waveform and passed to Faster-Whisper.

### TTS

File: `src/tts.py`

Responsibilities:

- Lazy-load a Piper voice.
- Cache short reusable phrases in memory.
- Return full WAV bytes for compatibility.
- Yield chunked PCM16 with `iter_synthesize_pcm16_chunks()`.

The live final-response path uses chunked Piper PCM output. The bridge converts
each chunk to PCMU/u-law and feeds the RTP pacer as chunks arrive, reducing the
delay before the caller hears the response.

### Session Management

File: `src/session_manager.py`

Responsibilities:

- Create and retrieve sessions.
- Persist session JSON when enabled.
- Enforce session timeout.
- Clear sessions on hangup or cleanup.

Durable state lives here. Real-time voice state stays in `PipecatHermesSkill`
because it is transient and timing-sensitive.

## 4. Request / Response Flow

### Caller Turn

1. Asterisk sends PCMU RTP to the bridge.
2. `src.media.ulaw_to_pcm16()` decodes 8 kHz PCMU to PCM16.
3. `src.media.upsample_8k_to_16k()` prepares audio for STT.
4. `PipecatHermesSkill.handle_incoming_audio()` buffers PCM and tracks energy.
5. `check_for_end_of_turn()` detects long pause or short pause after a cue.
6. `process_audio_turn()` calls `stt.transcribe_pcm16()`.
7. The bridge plays a short acknowledgement and thinking bleeps while the agent
   request runs.

### Agent Response

1. `route_message(..., synthesize_audio=False)` sends caller text to the agent.
2. The skill stores conversation history and the agent's response text.
3. The bridge reads `last_response_text_by_session`.
4. `tts.iter_synthesize_pcm16_chunks()` yields Piper PCM chunks.
5. `src.media.pcm16_to_ulaw_bytes()` converts each chunk to 8 kHz PCMU.
6. `RtpPlaybackPacer` queues and sends 20 ms RTP frames.
7. Playback state is cleared when the turn finishes or is interrupted.

## 5. Turn Taking and Barge-In

The system is mostly half-duplex for speakerphone reliability.

- Long pauses end ordinary turns.
- Short pauses can end turns after explicit handoff cues such as "over" or tag
  questions such as "right?" and "make sense?"
- A short acknowledgement plays quickly after a committed caller turn.
- Procedural thinking bleeps reduce dead air while Hermes or tools run.
- Long-wait spoken cues can play when the agent takes several seconds.
- Echo probe sets the acoustic profile:
  - `speakerphone`: strict half-duplex, keyword interrupt only.
  - `isolated`: lower echo, energy barge-in allowed.

Precise sentence-level interruption and resume timing remains future work.

## 6. Configuration and Setup

Configuration lives in `config/config.yaml` and is loaded by `src/config.py`.

Important runtime assets:

- Faster-Whisper model downloaded by its library/cache.
- Piper voice files in `models/`.
- Session JSON files in `sessions/` when persistence is enabled.

Setup helpers:

- `scripts/bootstrap.sh` creates the venv, installs dependencies, creates config,
  downloads the Piper voice, and runs preflight.
- `scripts/preflight.py` checks Python version, dependencies, config, model files,
  session directory writability, and optionally the Hermes endpoint.

Python 3.13 removed stdlib `audioop`; `requirements.txt` installs `audioop-lts`
for that module API.

## 7. Testing

Current lightweight tests:

- `tests/test_session_manager.py`
- `tests/test_turn_cues.py`
- `tests/test_media.py`

These avoid loading STT/TTS models, so they can run quickly in development:

```bash
python -m unittest discover -s tests -v
```

Live validation still requires the actual Asterisk/Hermes environment.

## 8. Current Status

| Component | Status | Notes |
| --- | --- | --- |
| Asterisk bridge | Working reference transport | ARI + ExternalMedia + RTP pacer |
| Skill orchestration | Working | Turn detection, routing, state, acknowledgements |
| STT | Working | Faster-Whisper, raw PCM live path |
| TTS | Working | Piper, cached phrases, chunked PCM live path |
| Media module | Working Python fallback | Optional `src._media_native` hook prepared |
| Session manager | Working | Optional JSON persistence and timeout cleanup |
| Agent client | Working | Hermes/OpenAI/Ollama-style HTTP support |
| Tests | Good lightweight coverage | Session, turn cue, and media tests |
| Live performance validation | Next step | Needs real Asterisk/Hermes call testing |

## 9. Near-Term Roadmap

1. Merge `asterisk-voice-agent-ux-skill` into `main`.
2. Run live-call validation on the target machine.
3. Add timing instrumentation for STT, agent latency, first TTS chunk, total TTS,
   RTP queue depth, and underruns.
4. Use those measurements to decide whether `src._media_native` is worth building.
5. Update README/repo naming to better express the Asterisk voice-agent UX role.

## 10. Open Questions

- Should the project be renamed around Asterisk voice-agent UX rather than Pipecat
  or Hermes?
- How much streaming should move into the agent layer if Hermes/Openclaw supports
  token streaming?
- Should the bridge remain single-call/single-port, or should multi-call RTP
  sessions become a near-term requirement?
- Should dynamic STT/TTS model swapping happen through config reloads, explicit
  admin commands, or process restart?
- If Rust is added, should it be a PyO3 module (`src._media_native`) or a separate
  sidecar process?

---

Last updated: June 2026, after the `asterisk-voice-agent-ux-skill` streaming/media-boundary pass.
