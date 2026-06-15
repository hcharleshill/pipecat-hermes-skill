"""
Small media conversion layer for the Asterisk bridge.

The functions here are intentionally narrow: PCM16, PCMU/u-law, WAV byte
decoding, resampling, and RTP-sized output chunks. If we add a compiled backend
later, it can provide the same function names via ``src._media_native`` and the
bridge does not need to change.
"""

from __future__ import annotations

import io
import logging
import struct
import wave
from typing import Iterable

try:
    import audioop
except ModuleNotFoundError as exc:
    raise RuntimeError(
        "Python 3.13 removed stdlib audioop. Install dependencies with "
        "`pip install -r requirements.txt` so audioop-lts provides it."
    ) from exc

try:
    from . import _media_native as _native
except Exception:
    _native = None


logger = logging.getLogger(__name__)

RTP_CHUNK_ULAW = 160
ULAW_SILENCE = b"\xff"


def _call_native(name: str, *args):
    if _native is None:
        return None
    fn = getattr(_native, name, None)
    if fn is None:
        return None
    return fn(*args)


def ulaw_to_pcm16(ulaw_data: bytes) -> bytes:
    """Convert PCMU/u-law bytes to little-endian signed 16-bit PCM."""
    if not ulaw_data:
        return b""
    native = _call_native("ulaw_to_pcm16", ulaw_data)
    if native is not None:
        return native
    return audioop.ulaw2lin(ulaw_data, 2)


def pcm16_to_ulaw(pcm16_data: bytes) -> bytes:
    """Convert little-endian signed 16-bit PCM to PCMU/u-law bytes."""
    if not pcm16_data:
        return b""
    native = _call_native("pcm16_to_ulaw", pcm16_data)
    if native is not None:
        return native
    return audioop.lin2ulaw(pcm16_data, 2)


def upsample_8k_to_16k(pcm8k: bytes) -> bytes:
    """Duplicate each 16-bit sample to convert 8 kHz PCM16 to 16 kHz PCM16."""
    if not pcm8k:
        return b""
    native = _call_native("upsample_8k_to_16k", pcm8k)
    if native is not None:
        return native
    even_len = len(pcm8k) & ~1
    out = bytearray(even_len * 2)
    out_pos = 0
    for i in range(0, even_len, 2):
        sample = pcm8k[i:i + 2]
        out[out_pos:out_pos + 2] = sample
        out[out_pos + 2:out_pos + 4] = sample
        out_pos += 4
    return bytes(out)


def downsample_16k_to_8k(pcm16k: bytes) -> bytes:
    """Average sample pairs to convert 16 kHz PCM16 to 8 kHz PCM16."""
    if not pcm16k:
        return b""
    native = _call_native("downsample_16k_to_8k", pcm16k)
    if native is not None:
        return native
    out = bytearray()
    for i in range(0, len(pcm16k) - 3, 4):
        a = struct.unpack_from("<h", pcm16k, i)[0]
        b = struct.unpack_from("<h", pcm16k, i + 2)[0]
        out.extend(struct.pack("<h", (a + b) // 2))
    return bytes(out)


def resample_pcm16(pcm16: bytes, in_rate: int, out_rate: int = 8000) -> bytes:
    """Resample mono little-endian PCM16."""
    if not pcm16 or in_rate <= 0:
        return b""
    if in_rate == out_rate:
        return pcm16
    native = _call_native("resample_pcm16", pcm16, in_rate, out_rate)
    if native is not None:
        return native
    converted, _ = audioop.ratecv(pcm16, 2, 1, in_rate, out_rate, None)
    return converted


def pcm16_rms(pcm16: bytes) -> float:
    """Root-mean-square energy for little-endian signed 16-bit mono PCM."""
    if not pcm16 or len(pcm16) < 2:
        return 0.0
    native = _call_native("pcm16_rms", pcm16)
    if native is not None:
        return float(native)
    try:
        sample_bytes = len(pcm16) & ~1
        samples = memoryview(pcm16[:sample_bytes]).cast("h")
        if not samples:
            return 0.0
        return (sum(s * s for s in samples) / len(samples)) ** 0.5
    except Exception:
        return 0.0


def wav_bytes_to_pcm16_and_rate(wav_bytes: bytes) -> tuple[bytes, int]:
    """Decode mono PCM WAV bytes and return (pcm16_bytes, sample_rate)."""
    if not wav_bytes:
        return b"", 22050
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            channels = wf.getnchannels()
            width = wf.getsampwidth()
            if channels != 1 or width != 2:
                logger.warning("Unexpected WAV format from TTS, attempting read anyway")
            rate = wf.getframerate() or 22050
            return wf.readframes(wf.getnframes()), rate
    except Exception as e:
        logger.error(f"Failed to decode WAV bytes: {e}")
        return b"", 22050


def wav_bytes_to_pcm16(wav_bytes: bytes) -> bytes:
    """Decode WAV bytes and return PCM16 payload."""
    pcm, _ = wav_bytes_to_pcm16_and_rate(wav_bytes)
    return pcm


def audio_bytes_to_ulaw(audio_bytes: bytes, pcm_sample_rate: int = 22050) -> bytes:
    """Convert WAV bytes or raw PCM16 bytes to 8 kHz PCMU/u-law bytes."""
    if not audio_bytes:
        return b""
    if audio_bytes[:4] == b"RIFF":
        pcm, in_rate = wav_bytes_to_pcm16_and_rate(audio_bytes)
        pcm8 = resample_pcm16(pcm, in_rate, 8000)
    elif pcm_sample_rate == 8000:
        pcm8 = audio_bytes
    else:
        pcm8 = resample_pcm16(audio_bytes, pcm_sample_rate, 8000)
    return pcm16_to_ulaw(pcm8)


def pad_ulaw_frame(chunk: bytes, frame_size: int = RTP_CHUNK_ULAW) -> bytes:
    """Pad or trim a u-law chunk to one RTP audio frame."""
    if len(chunk) >= frame_size:
        return chunk[:frame_size]
    return chunk + ULAW_SILENCE * (frame_size - len(chunk))


def iter_ulaw_frames(
    ulaw: bytes,
    *,
    frame_size: int = RTP_CHUNK_ULAW,
) -> Iterable[bytes]:
    """Yield padded RTP-sized u-law frames."""
    for i in range(0, len(ulaw), frame_size):
        yield pad_ulaw_frame(ulaw[i:i + frame_size], frame_size)


def pcm16_to_ulaw_bytes(
    pcm16: bytes,
    *,
    sample_rate: int,
    out_rate: int = 8000,
) -> bytes:
    """Convert raw PCM16 at sample_rate to u-law at out_rate."""
    if sample_rate != out_rate:
        pcm16 = resample_pcm16(pcm16, sample_rate, out_rate)
    return pcm16_to_ulaw(pcm16)


def iter_pcm16_to_ulaw_chunks(
    pcm_chunks: Iterable[tuple[bytes, int]],
    *,
    out_rate: int = 8000,
) -> Iterable[bytes]:
    """Convert an iterable of (pcm16_bytes, sample_rate) into u-law byte chunks."""
    for pcm16, sample_rate in pcm_chunks:
        if not pcm16:
            continue
        yield pcm16_to_ulaw_bytes(pcm16, sample_rate=sample_rate, out_rate=out_rate)
