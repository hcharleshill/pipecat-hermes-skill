import io
import struct
import unittest
import wave

from src import media


class TestMedia(unittest.TestCase):
    def test_ulaw_roundtrip_shapes(self):
        pcm = struct.pack("<hhhh", -1200, -100, 100, 1200)
        ulaw = media.pcm16_to_ulaw(pcm)
        decoded = media.ulaw_to_pcm16(ulaw)
        self.assertEqual(len(ulaw), len(pcm) // 2)
        self.assertEqual(len(decoded), len(pcm))

    def test_upsample_duplicates_samples(self):
        pcm8 = struct.pack("<hh", -1000, 1000)
        pcm16 = media.upsample_8k_to_16k(pcm8)
        self.assertEqual(
            struct.unpack("<hhhh", pcm16),
            (-1000, -1000, 1000, 1000),
        )

    def test_audio_bytes_to_ulaw_resamples_raw_pcm(self):
        pcm16k = struct.pack("<" + "h" * 160, *([500] * 160))
        ulaw = media.audio_bytes_to_ulaw(pcm16k, pcm_sample_rate=16000)
        self.assertGreater(len(ulaw), 0)
        self.assertLess(len(ulaw), len(pcm16k))

    def test_wav_decode(self):
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(22050)
            wf.writeframes(struct.pack("<hh", 1, -1))

        pcm, rate = media.wav_bytes_to_pcm16_and_rate(buf.getvalue())
        self.assertEqual(rate, 22050)
        self.assertEqual(pcm, struct.pack("<hh", 1, -1))

    def test_pad_ulaw_frame(self):
        self.assertEqual(media.pad_ulaw_frame(b"\x00", frame_size=4), b"\x00\xff\xff\xff")
        self.assertEqual(media.pad_ulaw_frame(b"12345", frame_size=4), b"1234")

    def test_pcm16_rms(self):
        pcm = struct.pack("<hh", 300, -300)
        self.assertAlmostEqual(media.pcm16_rms(pcm), 300.0)


if __name__ == "__main__":
    unittest.main()
