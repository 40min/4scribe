"""PCM16 mono 16 kHz helpers (stdlib wave). No resampling: PCM16 mono 16 kHz
is the accepted capture format; anything else is rejected explicitly."""
import base64, io, wave


def seconds_of(num_bytes: int, rate: int = 16000) -> float:
    return num_bytes / (2 * rate)


class InvalidAudio(ValueError):
    pass


def wrap_wav(pcm: bytes, rate: int = 16000) -> bytes:
    if not pcm:
        raise InvalidAudio("empty pcm")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def wav_b64(pcm: bytes, rate: int = 16000) -> bytes:
    """Base64 WAV description (the Ollama chat `images` path)."""
    return base64.b64encode(wrap_wav(pcm, rate))
