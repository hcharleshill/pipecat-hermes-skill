# Asterisk Voice Agent UX Skill

> Alpha (`v0.1.0-alpha`) - phone-call middleware for talking to an AI agent through Asterisk.

This project turns an Asterisk SIP call into a voice interface for a text-first
AI agent such as Hermes, Openclaw, Ollama, or an OpenAI-compatible gateway. It
handles the parts that make a phone conversation feel usable: turn detection,
STT, agent routing, TTS playback, acknowledgements, dead-air handling, barge-in,
session cleanup, and performance telemetry.

The repository is currently named `pipecat-hermes-skill` because it started as a
Hermes/Pipecat-adjacent skill. The working product shape is broader: an
Asterisk voice-agent UX layer.

## What It Does

```text
SIP phone
  -> Asterisk DTMF PIN gate
  -> Asterisk ARI + ExternalMedia
  -> RTP bridge
  -> Faster-Whisper STT
  -> Hermes/Openclaw/Ollama/OpenAI-compatible agent
  -> Piper TTS
  -> paced RTP playback
  -> SIP phone
```

## Features

- End-to-end phone calls through `asterisk_ari_bridge.py` using Asterisk Stasis,
  ExternalMedia, and PCMU/u-law RTP.
- Asterisk-side DTMF PIN gate before callers can reach ARI, STT, or the agent.
- Local STT with Faster-Whisper, using CUDA when available and CPU fallback.
- Local TTS with Piper, cached short phrases, and streaming PCM chunks for final responses.
- Voice UX behaviors: greeting, turn acknowledgements, thinking bleeps, long-wait cues,
  barge-in handling, and post-playback cooldown.
- Session persistence, hangup cleanup, and watchdog cleanup for stuck calls.
- Structured `perf` logs for STT, agent, TTS, RTP queue depth, backpressure, and underruns.
- Isolated `src.media` conversion module with an optional future native/Rust backend hook.

## Status

This is alpha software. It has been shaped around a single Asterisk + Linux +
NVIDIA development setup, and the live-call path should be validated on your
target machine before depending on it.

Known constraints:

- Single concurrent call / single RTP port.
- CPU-only STT works but is slower than CUDA.
- RTP playback quality may still need tuning on some networks/devices.
- The AI agent service is not bundled.
- Sample Asterisk config contains development credentials; change them before use.

## Quick Start

```bash
git clone <repo-url> pipecat-hermes-skill
cd pipecat-hermes-skill
./scripts/bootstrap.sh
cp config/config.example.yaml config/config.yaml
```

Edit `config/config.yaml` for your agent endpoint, backend, model, and API key.
Do not commit `config/config.yaml` with real secrets.

For full setup details, including GPU/CPU notes and Asterisk configuration, see
[INSTALL.md](INSTALL.md).

## Run The Bridge

```bash
source .venv/bin/activate

python asterisk_ari_bridge.py \
  --ari-url http://localhost:8088 \
  --ari-user asterisk \
  --ari-pass YOUR_ARI_PASSWORD \
  --rtp-host YOUR_LAN_IP \
  --rtp-port 16000
```

Recommended validation order:

1. Dial `100` for the Asterisk echo test.
2. Set `HERMES_AGENT_PIN` in Asterisk.
3. Run preflight against the deployed Asterisk config:

```bash
python scripts/preflight.py --asterisk-config-dir /etc/asterisk --require-agent-pin
```

4. Dial `101` for the PIN-protected live agent test.
5. Verify bad PINs never reach ARI or the agent.

## Configuration

Copy the example config:

```bash
cp config/config.example.yaml config/config.yaml
```

Important keys:

| Key | Purpose |
| --- | --- |
| `hermes.endpoint` | Agent HTTP base URL |
| `hermes.backend` | `openai`, `hermes`, or `ollama` |
| `hermes.model` | Agent/model name passed to compatible backends |
| `hermes.api_key` | Bearer token, if your gateway requires one |
| `session.persist_dir` | Runtime conversation JSON directory |

Asterisk caller authentication is configured in the Asterisk dialplan with
`HERMES_AGENT_PIN`, not in `config/config.yaml`.

## Observability

Live-call performance events are emitted on the `perf` logger as compact JSON.
Useful event names:

- `turn.accepted`
- `stt.transcribe`
- `agent.request`
- `tts.first_pcm`
- `tts.stream`
- `rtp.playback`
- `rtp.underrun`
- `rtp.backpressure`

These separate the major latency buckets: STT, agent request, time to first TTS
PCM, total TTS generation, RTP queue depth, and playback underruns.

## Tests

```bash
source .venv/bin/activate
python -m unittest discover -s tests -v
```

The lightweight tests avoid loading STT/TTS models, so they are intended to run
quickly during development.

## Project Layout

```text
asterisk_ari_bridge.py    # Asterisk ARI + RTP reference transport
ari.py                    # Minimal ARI WebSocket client shim
asterisk-config/          # Sample Asterisk dialplan, ARI, PJSIP, RTP config
config/
  config.example.yaml     # Copy to config.yaml; config.yaml is git-ignored
scripts/
  bootstrap.sh            # First-time setup
  download_piper_voice.sh # Piper voice model helper
  preflight.py            # Local/deployed setup checks
src/
  media.py                # PCM/u-law/resampling/RTP frame helpers
  pipecat_hermes_skill.py # Turn state, session state, agent routing
  session_manager.py      # Persistence and timeout cleanup
  stt.py                  # Faster-Whisper wrapper
  telemetry.py            # Structured perf logging helper
  tts.py                  # Piper wrapper
  thinking_verbs.py       # Long-wait spoken cue phrases
models/                   # Piper ONNX files; see models/README.md
tests/                    # Lightweight unit tests
```

## Security Notes

- Protect live extension `101` with `HERMES_AGENT_PIN` in Asterisk.
- Keep caller authentication in Asterisk so unauthenticated calls never reach the Python bridge.
- Change sample ARI/SIP passwords before using the configs outside local development.
- Keep API keys out of the repository.

## Roadmap

See [TODO.md](TODO.md). Near-term work is live-call validation, performance
profiling from the new telemetry, and deciding whether the `src.media` path
needs an optional native backend.

## License

MIT - see [LICENSE](LICENSE).
