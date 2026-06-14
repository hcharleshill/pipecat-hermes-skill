# Pipecat Hermes Skill

> **Alpha (`v0.1.0-alpha`)** — Local voice agent bridge: Asterisk SIP → Faster-Whisper STT → Hermes agent → Piper TTS. Tested on a single Linux + NVIDIA setup; see [Known issues](#known-issues).

A reusable session and messaging layer connecting **Hermes Agent** (or any OpenAI-compatible HTTP API) to real-time voice pipelines — with a working **Asterisk ARI + RTP** example for live phone calls.

Derived from patterns in the Hermes Telegram skill, adapted for turn-taking, barge-in, and low dead-air voice UX.

## Features (alpha)

- **End-to-end voice calls** via `asterisk_ari_bridge.py` (Stasis + ExternalMedia + μ-law RTP)
- **Local STT** — Faster-Whisper (`base`), CUDA with automatic CPU fallback
- **Local TTS** — Piper (`en_US-joe-medium`), in-memory phrase cache
- **Conversational UX** — call greeting, turn acks, cached thinking-bleep loop, long-wait spinner phrases
- **Session lifecycle** — persistence, hangup cleanup, in-process watchdog
- **Half-duplex / barge-in** — echo probe acoustic profiles, keyword interrupt on speakerphone

## Architecture

```
SIP Phone ──► Asterisk ──► ARI Bridge (RTP) ──► PipecatHermesSkill
                                                    ├─ STT (Faster-Whisper)
                                                    ├─ Hermes HTTP API
                                                    └─ TTS (Piper) ──► RTP playback
```

Full design: **[ARCHITECTURE.md](ARCHITECTURE.md)**

## Quick install

```bash
git clone <repo-url> pipecat-hermes-skill && cd pipecat-hermes-skill
./scripts/bootstrap.sh
# Edit config/config.yaml — Hermes endpoint + api_key
```

**Full guide (GPU, CPU-only, Asterisk, verification):** **[INSTALL.md](INSTALL.md)**

## Run the bridge

```bash
source .venv/bin/activate

python asterisk_ari_bridge.py \
  --ari-url http://localhost:8088 \
  --ari-user asterisk \
  --ari-pass YOUR_ARI_PASSWORD \
  --rtp-host YOUR_LAN_IP \
  --rtp-port 16000
```

Dial **100** (echo test) then **101** (live Hermes skill) from a SIP phone.

## Project layout

```
asterisk_ari_bridge.py    # ARI + RTP bridge (reference transport)
ari.py                    # Minimal ARI WebSocket client (vendored shim)
asterisk-config/          # Sample Asterisk dialplan + ARI + PJSIP
config/
  config.example.yaml     # Copy to config.yaml (git-ignored)
scripts/
  bootstrap.sh            # First-time setup
  download_piper_voice.sh
src/
  pipecat_hermes_skill.py # Core skill
  session_manager.py
  stt.py / tts.py
  thinking_verbs.py       # Long-wait voice phrases
models/                   # Piper ONNX (download — see models/README.md)
tests/                    # Unit tests (session manager, turn cues)
```

## Configuration

```bash
cp config/config.example.yaml config/config.yaml
```

| Key | Purpose |
|-----|---------|
| `hermes.endpoint` | Agent HTTP base URL |
| `hermes.backend` | `openai` \| `hermes` \| `ollama` |
| `hermes.api_key` | Bearer token (do not commit) |
| `session.persist_dir` | Conversation JSON (`sessions/`, git-ignored) |

## Tests

```bash
source .venv/bin/activate
python -m unittest discover -s tests -v
```

## Known issues

- RTP playback may still sound choppy on some paths (active tuning)
- CPU-only STT works but is noticeably slower than CUDA
- Single concurrent call / single RTP port
- Requires external Hermes (or compatible) gateway — not bundled
- Dev passwords in `asterisk-config/` — change before production

## Roadmap

See [TODO.md](TODO.md). Near-term: packaging polish, broader device testing, playback quality.

## License

MIT — see [LICENSE](LICENSE).

---

*Started June 2026 · Alpha release prep June 2026*