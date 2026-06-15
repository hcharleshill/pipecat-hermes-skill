# Pipecat Hermes Skill / Asterisk Voice Agent UX Skill - TODO

This repo currently acts as middleware between Asterisk phone calls and text-first
AI agents such as Hermes, Openclaw, Ollama, or OpenAI-compatible gateways. The
original Pipecat/Hermes name is still present in code and docs, but the working
product shape is an Asterisk voice-agent UX skill: turn taking, barge-in,
dead-air handling, STT, agent routing, TTS, and RTP playback.

## Recently Completed

- [x] Wire Asterisk ARI + ExternalMedia RTP into the skill.
- [x] Implement local Faster-Whisper STT and Piper TTS.
- [x] Add turn-boundary detection with long-pause and short-pause-after-cue logic.
- [x] Add basic barge-in / interruption handling.
- [x] Add session persistence, timeout cleanup, and lifecycle hooks.
- [x] Add Hermes/OpenAI/Ollama-style HTTP agent client support.
- [x] Add short acknowledgements, thinking bleeps, and long-wait spoken cues.
- [x] Add background TTS cache warmup.
- [x] Add background STT model warmup to reduce first-turn latency.
- [x] Remove temp-WAV use from live STT turns with `stt.transcribe_pcm16()`.
- [x] Add chunked Piper PCM output with `tts.iter_synthesize_pcm16_chunks()`.
- [x] Stream final TTS responses through the RTP pacer instead of waiting for a full WAV.
- [x] Move PCM/u-law/resampling/RMS/WAV helpers into `src.media`.
- [x] Add optional `src._media_native` hook for a future compiled media backend.
- [x] Add `audioop-lts` compatibility for Python 3.13+.
- [x] Add setup preflight checks in `scripts/preflight.py`.
- [x] Add media unit tests.
- [x] Add Asterisk-side DTMF PIN gate before `Stasis(hermes)`.
- [x] Add preflight checks for the Asterisk PIN gate and optional required `HERMES_AGENT_PIN`.
- [x] Add structured `perf` logging for STT transcription and agent request latency.
- [x] Add structured `perf` logging for streaming TTS and RTP queue/underrun behavior.

## Next Up

- [ ] Merge `asterisk-voice-agent-ux-skill` into `main` after review.
- [ ] Run live-call validation on the real Asterisk/Hermes host:
  - [ ] `python scripts/preflight.py --asterisk-config-dir /etc/asterisk --require-agent-pin`
  - [ ] `python -m unittest discover -s tests -v`
  - [ ] Dial `100` echo test.
  - [ ] Set `HERMES_AGENT_PIN` in Asterisk.
  - [ ] Dial `101` live agent test and verify bad PINs never reach ARI/the agent.
  - [ ] Check first-turn latency, response start time, choppy playback, hangup cleanup, and barge-in behavior.
- [x] Add timing instrumentation around the live call path:
  - [x] STT transcription latency.
  - [x] Hermes/agent request latency.
  - [x] Time to first TTS PCM chunk.
  - [x] Total TTS generation time.
  - [x] RTP queue depth and underrun/backpressure events.
- [x] Update README language toward "Asterisk Voice Agent UX Skill" while keeping Hermes/Pipecat history clear.
- [ ] Add optional caller-ID allowlist/rate-limit policy in Asterisk after PIN behavior is validated.

## Performance / Native Backend

- [ ] Profile `src.media` during real calls before writing Rust.
- [ ] If profiling shows media conversion or RMS work matters, build optional `src._media_native` with PyO3/maturin.
- [ ] Candidate native functions:
  - [ ] `ulaw_to_pcm16`
  - [ ] `pcm16_to_ulaw`
  - [ ] `upsample_8k_to_16k`
  - [ ] `resample_pcm16`
  - [ ] `pcm16_rms`
- [ ] Keep the Python `src.media` implementation as fallback.

## Agent / Model Flexibility

- [ ] Support dynamic STT model swapping:
  - [ ] Configurable Faster-Whisper model name.
  - [ ] Reload API for model changes.
  - [ ] GPU/CPU selection in config.
- [ ] Support dynamic TTS voice swapping:
  - [ ] Configurable Piper model path.
  - [ ] Reload API for voice changes.
  - [ ] Warm selected voice phrases after reload.
- [ ] Document Hermes, Openclaw, Ollama, and OpenAI-compatible backend expectations.

## Packaging / Release

- [ ] Add `pyproject.toml`.
- [ ] Add `CONTRIBUTING.md`.
- [ ] Add a GPU/CPU validation matrix.
- [ ] Add a short release checklist for alpha/beta tags.
- [ ] Decide whether/when to rename the project from Pipecat Hermes Skill to an Asterisk voice-agent name.

## Later

- [ ] Improve sentence-level TTS interruption and resume behavior.
- [ ] Consider streaming agent responses if the backend supports token streaming.
- [ ] Add richer observability export, such as Prometheus or structured JSON logs.
- [ ] Explore multiple simultaneous RTP sessions beyond the current single-port bridge design.

---

Last updated: June 2026, after the `asterisk-voice-agent-ux-skill` streaming/media-boundary pass.
