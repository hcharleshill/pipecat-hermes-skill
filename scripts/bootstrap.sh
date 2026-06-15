#!/usr/bin/env bash
# First-time project setup (venv, deps, config, Piper model)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-python3}"

if [[ ! -d .venv ]]; then
  echo "Creating virtualenv (.venv) ..."
  "$PY" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "Installing Python dependencies ..."
pip install --upgrade pip
pip install -r requirements.txt

if [[ ! -f config/config.yaml ]]; then
  echo "Creating config/config.yaml from example ..."
  cp config/config.example.yaml config/config.yaml
  echo "  → Edit config/config.yaml (Hermes endpoint, api_key)."
else
  echo "config/config.yaml already exists — leaving unchanged."
fi

mkdir -p sessions models
bash scripts/download_piper_voice.sh

echo "Running local preflight ..."
python scripts/preflight.py

echo ""
echo "Bootstrap complete. Next steps:"
echo "  1. Edit config/config.yaml"
echo "  2. Start Hermes gateway (see INSTALL.md)"
echo "  3. Configure Asterisk (see INSTALL.md)"
echo "  4. Run: source .venv/bin/activate"
echo "         python asterisk_ari_bridge.py --rtp-host YOUR_LAN_IP"
echo "  See INSTALL.md for full GPU/CPU and Asterisk setup."
