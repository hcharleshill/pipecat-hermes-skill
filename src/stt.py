"""
Local STT module using Faster-Whisper (base model).

This module provides a simple interface for the Pipecat Hermes Skill
to perform speech-to-text using a local model.
"""

from faster_whisper import WhisperModel
from typing import Optional
import logging
import numpy as np

from . import media as media_module

logger = logging.getLogger(__name__)

# Global model instance (lazy loaded)
_model: Optional[WhisperModel] = None


def get_model() -> WhisperModel:
    """Load and cache the Faster-Whisper base model.

    Prefers CUDA (float16) when the NVIDIA CUDA toolkit is installed.
    Falls back to CPU (int8) with a helpful message if CUDA libs are missing
    (common on systems that only have the driver, not the full toolkit).

    To enable GPU acceleration on Pop!_OS / Ubuntu:
        sudo apt update
        sudo apt install -y nvidia-cuda-toolkit
        sudo ldconfig
    Then restart the bridge.
    """
    global _model
    if _model is None:
        logger.info("Loading Faster-Whisper base model...")
        try:
            _model = WhisperModel("base", device="cuda", compute_type="float16")
            logger.info("Faster-Whisper base model loaded on CUDA (float16).")
        except Exception as e:
            logger.warning(
                f"CUDA backend failed to initialize ({type(e).__name__}: {e}). "
                "Falling back to CPU (int8). "
                "Install the CUDA toolkit for GPU STT: "
                "sudo apt install -y nvidia-cuda-toolkit && sudo ldconfig"
            )
            _model = WhisperModel("base", device="cpu", compute_type="int8")
            logger.info("Faster-Whisper base model loaded on CPU (int8).")
    return _model


def transcribe(audio_path: str, language: Optional[str] = None) -> str:
    """
    Transcribe an audio file using Faster-Whisper.

    Args:
        audio_path: Path to the audio file (wav, mp3, etc.)
        language: Optional language code (e.g., "en")

    Returns:
        Transcribed text.
    """
    model = get_model()
    segments, _ = model.transcribe(audio_path, language=language)
    return _segments_to_text(segments)


def transcribe_pcm16(
    pcm16: bytes,
    *,
    sample_rate: int = 16000,
    language: Optional[str] = None,
) -> str:
    """
    Transcribe raw mono PCM16 without writing a temporary WAV file.

    Faster-Whisper accepts a 16 kHz float32 waveform. Telephony input reaches
    the skill as 16 kHz mono PCM16, so this is the low-overhead live-call path.
    """
    if not pcm16:
        return ""
    if sample_rate != 16000:
        pcm16 = media_module.resample_pcm16(pcm16, sample_rate, 16000)
    sample_bytes = len(pcm16) & ~1
    if sample_bytes <= 0:
        return ""
    audio = np.frombuffer(pcm16[:sample_bytes], dtype="<i2").astype(np.float32)
    audio *= 1.0 / 32768.0
    model = get_model()
    segments, _ = model.transcribe(audio, language=language)
    return _segments_to_text(segments)


def _segments_to_text(segments) -> str:
    text = " ".join(segment.text for segment in segments)
    return text.strip()
