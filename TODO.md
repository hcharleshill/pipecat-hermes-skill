# Pipecat Hermes Skill – TODO

## High Priority

- [x] Replace pseudocode in `handle_incoming_audio()` with real Pipecat STT integration   (audio buffering + new process_audio_turn() using local stt module on temp WAV)
- [x] Replace pseudocode in `route_message()` with real Pipecat TTS integration   (local tts.synthesize + delivery via pipeline.send_audio or captured bytes)
- [x] Implement actual Hermes client call in `_send_to_hermes()`   (delegates to injected client supporting common shapes + error handling + safe fallbacks)
- [x] Add configuration loading from `config/config.yaml`   (fully implemented + global config)
- [x] Wire in Faster-Whisper (base model) for local STT   (module + full integration path complete)
- [x] Wire in Piper for local TTS   (module + full integration path complete)
- [x] Implement turn-boundary detection (initial):
  - Long pause → is_turn_complete() + process_if_turn_complete()
  - “Over” + short pause → _text_has_turn_cue() after STT
  - Tag question (“right?”, “yes?”, “okay?”, etc.) + short pause → same cue detector
  - Callers (transport) can poll process_if_turn_complete() or combine with cues
- [x] Implement interruption handling (basic):
  - Automatic energy-based barge-in detection in handle_incoming_audio while speaking
  - on_user_barge_in() + is_speaking state (transport can stop playback)
  - get_barge_in_acknowledgement() → "Go ahead." audio
  - get_resume_prompt() → "As I was saying. " + previous response
  - (Full sentence completion / precise remainder timing would require streaming TTS position tracking)

## Medium Priority

- [x] Implement proper session persistence   (SessionManager now supports optional JSON persistence in persist_dir + timeout enforcement from config; history survives restarts; update_and_persist() + cleanup_expired() added. Wired into PipecatHermesSkill using config.session.*)
- [x] Add logging and error handling   (error_handler enhanced with session context, recoverable flag, get_user_friendly_message helper; skill applies config logging more carefully; handle_error called with session_ids; _send_to_hermes now does 2-attempt retry with backoff for Hermes calls.)
- [x] Support multiple concurrent sessions   (RLock + protected sections already present; added end_session(), add_on_session_end() lifecycle hooks, cleanup_expired_sessions(), _cleanup_transient_for_expired integration, and clear state-separation design comments. Bridge now uses end_session(). Transient acoustic state intentionally kept separate from durable SessionManager for performance/reusability.)
- [x] Create unit tests for core routing logic   (created tests/test_session_manager.py (persistence, timeout, cleanup) + tests/test_turn_cues.py (isolated cue detector). All pass with stdlib only. Core routing paths now have basic coverage.)
- [x] Document how this skill differs from the original Telegram skill   (added detailed "Differences from the Original Hermes Telegram Skill" section to ARCHITECTURE.md covering transport, turn logic, persistence, concurrency, state separation, and tests)

## Future / Polish

- [x] Add metrics and observability hooks   (lightweight counters in PipecatHermesSkill: turns_processed, barge_ins, hermes_calls/errors, tts_syntheses, last_route_latency_ms + get_metrics() snapshot. No deps.)
- [ ] Support dynamic model swapping (STT/TTS)   (stt/tts use module-global lazy singletons; would need reload() APIs + config-driven paths)
- [x] Create example usage with Asterisk + Pipecat   (asterisk_ari_bridge.py provides a full working ARI + RTP bridge that feeds the skill, uses check_for_end_of_turn + acks/bleeps + end_session lifecycle)
- [x] Prepare for open-source release (alpha)   (MIT LICENSE; requirements.txt; config.example.yaml; INSTALL.md; bootstrap + download scripts; .gitignore for config/sessions/models; README refresh. Remaining: pyproject.toml, broader GPU/CPU test matrix, CONTRIBUTING.md.)
- [x] Provide a standard, reusable Asterisk configlet for the dialplan   (`asterisk-config/hermes.conf`: 100=Echo, 102=Milliwatt, 101=Stasis(hermes), documented in INSTALL.md)

---

*Last updated: June 2026 — Alpha pre-publish pass: INSTALL.md, requirements.txt, config.example.yaml, scripts/bootstrap.sh. Tag as v0.1.0-alpha for first GitHub push. Test on second machine (GPU) and CPU-only before beta.*