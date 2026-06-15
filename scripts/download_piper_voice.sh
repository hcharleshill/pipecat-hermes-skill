#!/usr/bin/env bash
# Download the default Piper voice (en_US-joe-medium) into models/
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODELS="$ROOT/models"
BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/joe/medium"

mkdir -p "$MODELS"
cd "$MODELS"

for f in en_US-joe-medium.onnx en_US-joe-medium.onnx.json; do
  if [[ -f "$f" ]]; then
    echo "Already present: $f"
  else
    echo "Downloading $f ..."
    curl -fL -o "$f" "$BASE/$f"
  fi
done

echo "Done. Piper model files are in $MODELS"