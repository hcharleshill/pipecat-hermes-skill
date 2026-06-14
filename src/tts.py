"""
Local TTS module using Piper.

This module provides a simple interface for the Pipecat Hermes Skill
to perform text-to-speech using a local Piper model.
"""

from piper import PiperVoice
from typing import Optional
import io
import logging
import threading
import wave
from pathlib import Path

logger = logging.getLogger(__name__)

# Global voice instance (lazy loaded)
_voice: Optional[PiperVoice] = None

# In-memory WAV cache for fixed phrases (spinner verbs, acks, greeting, etc.)
_tts_cache: dict[str, bytes] = {}
_tts_cache_lock = threading.Lock()
# Do not cache long dynamic Hermes responses — only short reusable phrases.
_TTS_CACHE_MAX_TEXT_LEN = 256

# Default model path
# Project default: en_US-joe-medium (chosen voice for Hermes TTS).
# Other options (more "natural" per some listeners): en_US-amy-medium, en_US-ryan-medium, etc.
# Browse samples: https://rhasspy.github.io/piper-samples/
# Downloads: https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_US
# Place BOTH the .onnx and the matching .onnx.json in models/
DEFAULT_MODEL_PATH = Path(__file__).parent.parent / "models" / "en_US-joe-medium.onnx"


def get_voice(model_path: Optional[str] = None) -> PiperVoice:
    """Load and cache the Piper voice model."""
    global _voice
    if _voice is None:
        path = Path(model_path) if model_path else DEFAULT_MODEL_PATH
        logger.info(f"Loading Piper voice from {path}...")
        _voice = PiperVoice.load(str(path))
        logger.info("Piper voice loaded.")
    return _voice


def _cache_key(text: str, model_path: Optional[str] = None) -> str:
    path = str(model_path) if model_path else str(DEFAULT_MODEL_PATH)
    return f"{path}\0{text.strip()}"


def _should_cache(text: str, use_cache: bool) -> bool:
    return use_cache and 0 < len(text.strip()) <= _TTS_CACHE_MAX_TEXT_LEN


def _synthesize_wav_bytes_uncached(text: str, model_path: Optional[str] = None) -> bytes:
    voice = get_voice(model_path)
    chunks = list(voice.synthesize(text))
    buf = io.BytesIO()
    if not chunks:
        with wave.open(buf, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(22050)
            wav_file.writeframes(b"")
    else:
        pcm = b"".join(getattr(c, "audio_int16_bytes", b"") for c in chunks)
        sr = getattr(chunks[0], "sample_rate", 22050) or 22050
        with wave.open(buf, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sr)
            wav_file.writeframes(pcm)
    return buf.getvalue()


def synthesize_to_wav_bytes(
    text: str,
    model_path: Optional[str] = None,
    *,
    use_cache: bool = True,
) -> bytes:
    """
    Synthesize speech and return WAV bytes.

    Short fixed phrases (spinner verbs, acks, greeting) are cached in memory
    after the first synthesis so Piper is not run again for the same text.
    """
    normalized = text.strip()
    cacheable = _should_cache(normalized, use_cache)
    key = _cache_key(normalized, model_path) if cacheable else None

    if cacheable:
        with _tts_cache_lock:
            cached = _tts_cache.get(key)
        if cached is not None:
            logger.debug(f"TTS cache hit: {normalized[:60]!r}")
            return cached

    data = _synthesize_wav_bytes_uncached(normalized or text, model_path)

    if cacheable and key is not None:
        with _tts_cache_lock:
            _tts_cache[key] = data
        logger.debug(f"TTS cache store: {normalized[:60]!r}")

    return data


def warm_cache(phrases: list[str], model_path: Optional[str] = None) -> int:
    """Pre-synthesize and cache a list of short phrases. Returns count warmed."""
    warmed = 0
    for phrase in phrases:
        text = phrase.strip()
        if not text or len(text) > _TTS_CACHE_MAX_TEXT_LEN:
            continue
        key = _cache_key(text, model_path)
        with _tts_cache_lock:
            if key in _tts_cache:
                continue
        synthesize_to_wav_bytes(text, model_path, use_cache=True)
        warmed += 1
    return warmed


def cache_size() -> int:
    with _tts_cache_lock:
        return len(_tts_cache)


def synthesize(
    text: str,
    output_path: str,
    model_path: Optional[str] = None,
    *,
    use_cache: bool = True,
):
    """
    Synthesize speech from text using Piper.

    All TTS in the project (static test greetings + dynamic responses from the skill)
    now defaults to the chosen voice: en_US-joe-medium.

    Args:
        text: Text to synthesize.
        output_path: Path to save the generated WAV file.
        model_path: Optional path to override the default Piper model.
        use_cache: Cache short phrases in memory (disable for long dynamic responses).
    """
    data = synthesize_to_wav_bytes(text, model_path, use_cache=use_cache)
    with open(output_path, "wb") as f:
        f.write(data)
    logger.info(f"TTS output written to {output_path}")