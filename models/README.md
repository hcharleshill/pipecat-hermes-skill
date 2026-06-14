# Piper voice models

TTS models are **not** shipped in the repository (large binary files).

## Default voice: `en_US-joe-medium`

Download **both** files into this directory:

- [en_US-joe-medium.onnx](https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/joe/medium/en_US-joe-medium.onnx)
- [en_US-joe-medium.onnx.json](https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/joe/medium/en_US-joe-medium.onnx.json)

Or run from the project root:

```bash
./scripts/download_piper_voice.sh
```

Browse other English voices: [Piper samples](https://rhasspy.github.io/piper-samples/)

Expected layout:

```
models/
  en_US-joe-medium.onnx
  en_US-joe-medium.onnx.json
```