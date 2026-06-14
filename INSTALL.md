# Installation Guide

**Status:** Alpha (`v0.1.0-alpha`) — tested primarily on Linux with Asterisk (Podman),
Linphone, NVIDIA GPU, and a local Hermes API gateway.

This guide is written for a **fresh machine** so you can validate GPU and CPU-only setups.

---

## What you are installing

| Component | Role |
|-----------|------|
| **Pipecat Hermes Skill** | STT → Hermes agent → TTS orchestration |
| **asterisk_ari_bridge.py** | Asterisk ARI + RTP ↔ skill (SIP phone calls) |
| **Faster-Whisper** | Local speech-to-text |
| **Piper** | Local text-to-speech |
| **Hermes gateway** | External agent (OpenAI-compatible HTTP API) |
| **Asterisk** | SIP + Stasis + ExternalMedia |

---

## Prerequisites

### All installs

- **Python 3.11+** (3.12 tested)
- **curl**, **git**
- **~2 GB disk** for Whisper/Piper models (downloaded on first run / via script)
- **Hermes agent** (or OpenAI-compatible gateway) running and reachable on HTTP
- **Asterisk 18+** with ARI + `res_ari_external_media` (Podman example below)

### GPU path (recommended for STT latency)

- NVIDIA GPU with driver installed
- **CUDA toolkit** for Faster-Whisper (ctranslate2):
  ```bash
  sudo apt update
  sudo apt install -y nvidia-cuda-toolkit
  sudo ldconfig
  ```
- On first bridge run, logs should show:
  `Faster-Whisper base model loaded on CUDA (float16).`

### CPU-only path (usable, slower STT)

- No GPU required
- Faster-Whisper **automatically falls back** to `device=cpu, compute_type=int8`
- Expect **1–3 s** turn transcription vs sub-second on GPU for short utterances
- Piper TTS is CPU-only already — fine on all machines

---

## Quick start (project bootstrap)

```bash
git clone <your-repo-url> pipecat-hermes-skill
cd pipecat-hermes-skill

./scripts/bootstrap.sh
```

This creates `.venv`, installs dependencies, copies `config/config.example.yaml` →
`config/config.yaml` (if missing), and downloads the default Piper voice.

Edit `config/config.yaml`:

```yaml
hermes:
  endpoint: "http://localhost:8080"
  backend: "openai"
  model: "hermes-agent"
  api_key: "your-gateway-token"
```

---

## Manual install (step by step)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

cp config/config.example.yaml config/config.yaml
# edit config/config.yaml

./scripts/download_piper_voice.sh
mkdir -p sessions
```

Run unit tests (stdlib + pydantic only for some tests; full suite needs venv):

```bash
python -m unittest discover -s tests -v
```

---

## Hermes gateway

The bridge expects an HTTP API at `config.yaml → hermes.endpoint`.

**OpenAI-compatible** (`backend: openai`):

```
POST {endpoint}/v1/chat/completions
Authorization: Bearer {api_key}
```

Start your Hermes gateway per its own docs, then verify:

```bash
curl -s http://localhost:8080/health || curl -s http://localhost:8080/
```

---

## Asterisk setup

Sample configs live in `asterisk-config/`:

| File | Purpose |
|------|---------|
| `hermes.conf` | Dialplan: **100** = Echo test, **101** = live skill |
| `ari.conf` | ARI user (change dev password before production) |
| `pjsip.conf` | Example SIP endpoints |
| `http.conf` | HTTP server for ARI |

Include the dialplan in `extensions.conf`:

```
#include /path/to/asterisk-config/hermes.conf
```

### Podman (example)

Adjust image/volumes to your environment:

```bash
# Example only — see your existing container setup
podman run -d --name asterisk-hermes \
  -p 5060:5060/udp -p 8088:8088/tcp -p 10000-10100:10000-10100/udp \
  -v ./asterisk-config:/etc/asterisk:Z \
  docker.io/andrius/asterisk:latest
```

Enable ARI in `ari.conf` and set a **non-default password** before exposing to a network.

---

## Running the bridge

From the project root (so `ari.py` and `src/` resolve):

```bash
source .venv/bin/activate

python asterisk_ari_bridge.py \
  --ari-url http://localhost:8088 \
  --ari-user asterisk \
  --ari-pass YOUR_ARI_PASSWORD \
  --rtp-host YOUR_LAN_IP \
  --rtp-port 16000
```

**Important:** `--rtp-host` must be the IP Asterisk can reach (your host LAN IP, not
`127.0.0.1` when Asterisk runs in a container).

Only **one** bridge instance can bind UDP 16000.

---

## Verification checklist

Use a SIP softphone (e.g. Linphone) registered to Asterisk.

| Step | Extension | Expected |
|------|-----------|----------|
| 1 | **100** | Echo — hear yourself (media path OK) |
| 2 | **101** | Greeting: *"Hello. This is Hermes…"* |
| 3 | Speak a sentence | Short ack → thinking tones → Hermes reply |
| 4 | Hang up | Logs: `Session stopped and resources released` |
| 5 | Call again | Clean new session, no bleed from prior call |

### GPU vs CPU check

Watch bridge logs on first transcription:

- GPU: `Faster-Whisper base model loaded on CUDA (float16).`
- CPU: `Falling back to CPU (int8).`

---

## Known issues (alpha)

- **Playback quality:** RTP pacing and resampling have been improved but may still
  sound choppy on some networks/phones — active area of work.
- **Half-duplex:** Speakerphone mode uses strict half-duplex; say *"stop"* or *"wait"*
  to interrupt. Headset/isolated mode allows energy barge-in.
- **Hermes latency:** Long tool loops can delay voice responses; spinner phrases play
  after ~7 s.
- **Python 3.13:** `audioop` deprecation warning in bridge (stdlib removal planned).
- **Single call:** Bridge handles one RTP session at a time by design.

---

## Troubleshooting

| Symptom | Things to check |
|---------|------------------|
| No audio on 101 | Dial **100** first; verify `--rtp-host` LAN IP; Asterisk firewall/ports |
| `UDP port 16000 already in use` | Kill other bridge: `pgrep -af asterisk_ari_bridge` |
| STT always CPU | Install `nvidia-cuda-toolkit`, restart bridge |
| Hermes silent / errors | `curl` gateway; check `api_key` and `backend` in config |
| Piper error on start | Run `./scripts/download_piper_voice.sh` |
| Session stuck after hangup | Watchdog should reap within 90 s; check ARI `StasisEnd` logs |

---

## Publishing / development notes

- Do **not** commit `config/config.yaml` (use example template).
- `sessions/` and `models/*.onnx` are git-ignored.
- Default Asterisk passwords in `asterisk-config/` are **dev-only**.

See [ARCHITECTURE.md](ARCHITECTURE.md) for design detail and [README.md](README.md) for overview.