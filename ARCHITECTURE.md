# Pipecat Hermes Skill — Architecture

## 1. Overview

The **Pipecat Hermes Skill** is a reusable adapter layer that connects a **Hermes Agent** (a text-oriented intelligent agent) to real-time voice pipelines built with **Pipecat**.

Its primary responsibilities are:

- Message routing between voice (audio) and the Hermes Agent (text)
- Conversation session management and context
- Abstraction of transport differences (originally Telegram, now voice/Pipecat)
- Integration with local STT (Speech-to-Text) and TTS (Text-to-Speech) models

This skill is intended to serve as the foundation for higher-level integrations, most notably an **Asterisk-Hermes bridge** for telephony-based voice agents.

The project is derived from patterns established in the Hermes Telegram skill and adapted for the constraints and opportunities of voice (streaming audio, turn-taking, interruptions, low-latency requirements).

**Status**: Core routing, STT/TTS integration, turn-taking, barge-in, persistence, and concurrency are implemented and used by the live Asterisk ARI bridge (`asterisk_ari_bridge.py`). See TODO.md for remaining polish items.

---

## 2. Goals

- Provide a clean, reusable interface between Hermes Agents and Pipecat voice pipelines.
- Centralize session lifecycle and conversation history management.
- Abstract away the differences between text-based frontends (e.g. Telegram) and voice frontends.
- Enable easy integration of local, private STT/TTS models.
- Serve as a reusable "skill" component that can be composed into larger systems (especially Asterisk + Pipecat).
- Support natural conversational behaviors: turn-boundary detection and barge-in/interruption handling.

**Non-Goals** (current):
- Implementing the full Pipecat pipeline itself.
- Implementing the Hermes Agent runtime.
- Handling raw telephony (Asterisk channel management, SIP, etc.).
- Providing production-grade observability, scaling, or persistence (planned for later).

---

## Differences from the Original Hermes Telegram Skill

This skill reuses the core patterns (message routing, `SessionManager`, error handling) from the Telegram skill but adapts them for real-time voice:

- **Transport abstraction**: Telegram skill dealt with discrete text messages over the Telegram Bot API. This skill receives streaming raw PCM audio chunks (`handle_incoming_audio`) and must synthesize audio responses.
- **Turn taking**: Added first-class support for voice-specific turn boundary detection (long pause, explicit "Over", tag questions + short pause) plus energy-based barge-in / interruption handling with "Go ahead." acks and "As I was saying..." resume prompts.
- **STT / TTS integration**: Local Faster-Whisper (file-based via temp WAV) and Piper are wired directly. The Telegram skill had no audio processing.
- **Listening / filler noises**: New lightweight mechanisms (`get_turn_acknowledgement` + `generate_listening_bleeps`) to avoid dead air. Short verbal acks ("Gotcha.", "Well...") followed by cheap random-pitch 5 ms click loops when Hermes is slow.
- **Session persistence**: `SessionManager` now supports optional JSON file persistence per session + automatic timeout enforcement (from `config.session.timeout_seconds`). Conversation history survives process restarts (the Telegram skill was purely in-memory).
- **Concurrent sessions**: Added `threading.RLock` protection around per-session audio buffers, speaking state, and turn decision paths so multiple simultaneous conversations are safe.
- **State separation**: Conversation history + metadata live in the (now persistent) `SessionManager`. Transient audio buffers, VAD timing, and barge-in state remain ephemeral in the skill instance.
- **Configuration**: Extended `SessionConfig` with `persist` / `persist_dir`. Logging level from config is applied at skill initialization.
- **Unit tests**: New `tests/` coverage for the new persistence, timeout, cleanup, and cue-detection logic (stdlib only, no model dependencies).

The Telegram skill was primarily a text router + session store. This one is a voice bridge with acoustic turn logic, low-cost filler audio, and durable sessions.

---

## 3. System Context

```
┌─────────────────────┐
│   Hermes Agent      │  (text intelligence, tools, memory, reasoning)
└──────────┬──────────┘
           │ Text (request/response)
           ▼
┌─────────────────────────────────────┐
│   Pipecat Hermes Skill (this repo)  │  ◄── Session management, routing, turn logic
│   - PipecatHermesSkill              │
│   - SessionManager                  │
│   - STT / TTS adapters              │
└──────────┬──────────────────────────┘
           │ Text + Audio control
           ▼
┌─────────────────────────────────────┐
│   Pipecat Pipeline                  │  (audio framing, VAD, streaming)
└──────────┬──────────────────────────┘
           │ Audio (raw or framed)
           ▼
┌─────────────────────────────────────┐
│   Audio Transport Layer             │
│   (Pipecat transport / future       │
│    Asterisk bridge)                 │
└──────────┬──────────────────────────┘
           │
           ▼
      STT ↔ TTS (local models)
```

**Key external interfaces**:
- **Hermes client**: Injected into the skill; responsible for sending user text to the Hermes Agent and returning the agent's textual response.
- **Pipecat pipeline / transport**: Supplies incoming audio chunks and consumes outgoing synthesized audio. May also expose VAD or other audio primitives in the future.

---

## 4. High-Level Architecture

The skill follows a layered adapter pattern:

1. **Transport / Audio Layer** (outside this module)
   - Receives audio from microphone, WebRTC, Asterisk, etc.
   - Provides raw or framed audio chunks to the skill.

2. **Voice I/O Layer** (this module — STT/TTS adapters)
   - `stt.py`: Converts audio → text using Faster-Whisper.
   - `tts.py`: Converts text → audio using Piper.

3. **Orchestration & Routing Layer** (`PipecatHermesSkill`)
   - Entry points: `handle_incoming_audio()` and `route_message()`.
   - Owns the conversation loop for a session.
   - Coordinates STT → Hermes → TTS.

4. **Session & State Layer**
   - `SessionManager` (standalone, reusable).
   - Inline session dict in the main skill (current, simpler implementation).

5. **Configuration & Cross-cutting**
   - `config.py` (Pydantic models + YAML loader).
   - `error_handler.py` (basic logging wrapper).

The design deliberately separates concerns so that:
- STT/TTS implementations can be swapped.
- Session management can be upgraded independently.
- The same routing/session patterns can be reused across different transports (Telegram, voice, future others).

---

## 5. Core Components

### `PipecatHermesSkill` (main orchestrator)
- Located in `src/pipecat_hermes_skill.py`.
- Constructor takes `hermes_client` and `pipecat_pipeline` (both currently stubs).
- Public methods:
  - `handle_incoming_audio(session_id, audio_chunk: bytes)` — receives audio from the voice side.
  - `route_message(session_id, message: str)` — processes a completed text utterance.
- Internal:
  - `_send_to_hermes(message)` — placeholder for Hermes client call.
  - Very basic in-memory session storage.

This class is the primary integration point consumers will use.

### `SessionManager`
- Located in `src/session_manager.py`.
- Reusable component originally from the Telegram skill.
- Provides `get_or_create(session_id)` and `clear(session_id)`.
- Currently more feature-complete than the inline session handling inside `PipecatHermesSkill`.
- Stores: `id`, `history`, `metadata`, and (planned) timeout handling.

### STT Module (`stt.py`)
- Wraps `faster_whisper.WhisperModel` (base model by default).
- Lazy-loads the model globally.
- Current API: `transcribe(audio_path: str, language=None) -> str`.
- Designed for file-based input today; will need streaming/chunk adaptation for real voice use.

### TTS Module (`tts.py`)
- Wraps `piper.PiperVoice`.
- Lazy-loads the voice model.
- Current API: `synthesize(text, output_path, model_path=None)`.
- Writes WAV files to disk. Will need streaming or in-memory audio buffer output for low-latency voice.

### Configuration (`config.py`)
- Uses Pydantic `BaseModel` for validation.
- Top-level `PipecatHermesConfig` containing:
  - `hermes`: endpoint
  - `pipecat.stt` / `pipecat.tts`
  - `session.timeout_seconds`
  - `logging.level`
- `load_config()` defaults to `config/config.yaml` relative to the project.
- A module-level `config` instance is loaded on import.

### Error Handling (`error_handler.py`)
- Currently a thin logging wrapper.
- Intended to centralize reusable error patterns from the Telegram skill.

---

## 6. Request / Response Flow (Target)

1. **Audio Ingestion**
   - `handle_incoming_audio(session_id, audio_chunk)` is called with raw audio bytes.
   - Audio is buffered or passed to VAD/turn detector.

2. **Turn Boundary Detection** (not yet implemented)
   - System decides a user turn has completed using one or more signals:
     - Significant silence / long pause.
     - Explicit cue: "Over".
     - Tag question + pause ("right?", "okay?").
   - Partial transcripts may be generated for barge-in detection.

3. **Speech-to-Text**
   - Completed audio for the turn is transcribed (using `stt.transcribe` or a future streaming variant).

4. **Session Retrieval**
   - `SessionManager.get_or_create(session_id)` (or equivalent) is called.
   - Conversation history is loaded/updated.

5. **Routing to Hermes**
   - `route_message(session_id, text)` is invoked.
   - The text + session context is sent to Hermes via the injected `hermes_client`.
   - Hermes returns a textual response (possibly with tool use, structured output, etc.).

6. **Text-to-Speech**
   - Response text is synthesized using the TTS module.
   - Audio is returned to the Pipecat pipeline / transport for playback.

7. **Interruption / Barge-in Handling** (future)
   - While TTS is playing, new audio from the user can interrupt.
   - Current sentence is finished (or aborted).
   - System acknowledges ("Go ahead.") and may resume with "As I was saying..." + remainder.
   - Limited full-duplex window (~10 seconds) is supported.

---

## 7. Session Management

Sessions are the unit of conversation state.

Current implementation (in `PipecatHermesSkill`):
- Simple dict: `session_id → {"id": ..., "history": []}`

Standalone `SessionManager` (preferred long-term):
- Richer structure with `metadata`.
- Designed for easy extension (persistence, timeout, metrics).

Planned enhancements (from TODO.md):
- Proper session persistence (beyond in-memory dict).
- Support for multiple concurrent sessions with isolation.
- Session timeout (currently configured at 300s but not enforced in code).
- History trimming or summarization for long conversations.

---

## 8. Configuration Management

Configuration is centralized and validated:

- **File**: `config/config.yaml`
- **Loader**: `src/config.py:load_config()`
- **Models**: Strongly typed via Pydantic (`STTConfig`, `TTSConfig`, `HermesConfig`, etc.).

The global `config` object is populated at import time. This is convenient for simple usage but may need to become injectable for testing and multi-tenant scenarios.

Default expectations:
- Hermes reachable at `http://localhost:8080`
- STT: Faster-Whisper "base"
- TTS: Piper (model not bundled — see `.gitignore`)

---

## 9. STT and TTS Subsystems

### Design Choices
- **Local models only** (for now): Faster-Whisper + Piper.
  - Benefits: privacy, low latency, no API keys, works offline.
  - Trade-off: higher CPU/GPU requirements on the host.
- Lazy loading of heavy models to reduce startup time and memory when not needed.
- Currently **file-oriented** APIs. Real voice use cases will require:
  - Streaming audio input for STT (or chunked processing).
  - Streaming or low-latency in-memory audio output for TTS (instead of writing files).

### Model Management
- Piper model path defaults to `models/en_US-joe-medium.onnx` (the project-chosen voice for Hermes; other Piper models can be swapped in).
- Large model files are git-ignored. Users must download them separately.
- Future work may include model swapping at runtime (see TODO: "Support dynamic model swapping").

---

## 10. Error Handling & Resilience

Current state is minimal:
- `handle_error()` only logs.
- No retries, circuit breakers, or graceful degradation yet.

Planned (from TODO):
- Structured error handling reused from Telegram skill.
- Appropriate behavior on STT/TTS failures (e.g., "I'm having trouble hearing you").
- Handling of Hermes client unavailability.

---

## 11. Integration Points & Extension

### Primary Extension Points
1. **Hermes Client**
   - The skill expects an object passed to `__init__` that can send messages and return responses.
   - Current expectation (from comments): similar interface to the Telegram skill's Hermes integration.

2. **Pipecat Pipeline**
   - The skill is given a `pipecat_pipeline` object.
   - It is expected to eventually expose `.stt()`, `.tts()`, and `.send_audio()` capabilities (currently pseudocode).

3. **Audio Transport**
   - `handle_incoming_audio` is the main entry point for new audio.
   - Future Asterisk integration will likely call this method (or a higher-level wrapper).

4. **SessionManager**
   - Can be used independently or swapped for a persistent implementation.

5. **STT / TTS Modules**
   - Can be extended or replaced by implementing the same function signatures or by subclassing/adapting inside the main skill.

### Reusability Goal
The explicit intent is that routing, session, and error-handling logic can be shared across different "skills" (Telegram, voice/Pipecat, future Asterisk, etc.).

---

## 12. Current Implementation Status

| Component                  | Implementation Level          | Notes |
|----------------------------|-------------------------------|-------|
| `PipecatHermesSkill`       | Complete (core flows)         | Full `handle_incoming_audio`, turn detection (`check_for_end_of_turn`), `process_audio_turn`, `route_message`, `_send_to_hermes` (with client tolerance + retries), TTS delivery, last_response_*. |
| `SessionManager`           | Complete + persistence        | In-memory + optional JSON-per-session, timeout enforcement, `cleanup_expired`, `update_and_persist`. |
| STT (`stt.py`)             | Working (integrated)          | Faster-Whisper base wired end-to-end via temp WAV in turn processing. |
| TTS (`tts.py`)             | Working (integrated)          | Piper (joe voice) wired; also used for acks and barge-in prompts. |
| Configuration              | Complete                      | Pydantic models + YAML. Global `config` on import. Logging level applied. |
| Error Handler              | Solid                         | Full tracebacks, optional session_id + recoverable flag, `get_user_friendly_message` helper. |
| Turn detection / barge-in  | Complete                      | Long pause (1.2s) + short-pause-after-cue (~0.65s), comprehensive tag questions + "over", energy barge-in, acks + resume prompts, procedural listening bleeps. |
| Hermes client call         | Complete                      | Injected client supporting common shapes; 2-attempt retry + backoff on transient failure. |
| Concurrency / Lifecycle    | Complete (for intended use)   | RLock on transient state; `end_session()`, `add_on_session_end()` hooks; transient vs. durable state separation documented. |
| Asterisk Example           | Working                       | `asterisk_ari_bridge.py` implements full ARI + RTP round-trip using the skill. |
| Tests                      | Good (core logic)             | `tests/test_session_manager.py` + `tests/test_turn_cues.py` (stdlib only, no model deps). |
| Documentation              | Good                          | README, ARCHITECTURE.md (with differences from Telegram skill), TODO.md kept in sync. |

All high- and medium-priority items from `TODO.md` are complete. The system is usable for real voice conversations (via the Asterisk bridge or a custom Pipecat transport).

---

## 13. Roadmap & Open Questions

### Near-term (High/Medium Priority per TODO) — Completed
- Wire real STT and TTS, Hermes client, turn-boundary detection, barge-in handling, persistence, concurrency, and lifecycle all complete (see updated status table above).
- Remaining items are now in the Future/Polish section of TODO.md.

### Medium-term
- Replace in-memory session storage with proper persistence.
- Add logging, structured error handling, and metrics.
- Support concurrent sessions safely.
- Write unit tests for routing and session logic.
- Document concrete differences from the original Telegram skill.

### Longer-term / Strategic
- Streaming STT/TTS support for lower latency.
- Dynamic model loading / hot-swapping.
- Full Asterisk + Pipecat example integration.
- Observability hooks (tracing, audio quality metrics, conversation analytics).
- Preparation for open-source release.

### Open Architectural Questions
- Should `PipecatHermesSkill` own a `SessionManager` instance, or should the caller compose them?
- Async vs. sync design: Voice pipelines are often async (asyncio). The current skeleton is synchronous.
- How much audio buffering / VAD logic should live inside this skill vs. in the Pipecat transport layer?
- What is the exact interface contract for the injected `hermes_client` and `pipecat_pipeline`?
- Should the skill support both request/response and event-driven (streaming) response styles from Hermes?

---

*This document should be updated as the implementation evolves and as major design decisions are made.*