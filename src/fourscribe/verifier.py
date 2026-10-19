"""Pre-decode speech verification seam (Silero VAD, pinned 6.2.3 JIT).

Owner trial: one shared verifier instance is created by the app and handed to
every session; the verifier lock + the single-worker vad executor serialize
stateful Silero reset/inference so two streams can never race the model state.
The verifier is stateless from the session's point of view: it runs on the
EXACT segment PCM and does not trim, re-time, or mutate session buffers.
"""
import threading

VERIFIER_NAME = "silero-vad-6.2.3"
#: predeclared balanced config from the isolated shadow experiment
SILERO_THRESHOLD = 0.5
SILERO_MIN_SPEECH_MS = 96


class VerifierUnavailable(RuntimeError):
    pass


class NullVerifier:
    """Accept-all fallback used only when verification is explicitly disabled
    or by direct session/test construction. Never a silent rejection path."""
    name = "null"
    enabled = False
    available = False

    def verify(self, pcm: bytes) -> bool:
        return True


class SileroVerifier:
    name = VERIFIER_NAME
    enabled = True
    available = True

    def __init__(self, model, speech_timestamps, threshold=SILERO_THRESHOLD,
                 min_speech_ms=SILERO_MIN_SPEECH_MS):
        self._model = model
        self._fn = speech_timestamps
        self.threshold = threshold
        self.min_speech_ms = min_speech_ms
        self._lock = threading.Lock()

    def verify(self, pcm: bytes) -> bool:
        usable = len(pcm) - (len(pcm) % 2)
        if usable <= 0:
            return False
        import numpy as np
        import torch

        samples = np.frombuffer(pcm[:usable], dtype=np.int16).astype(np.float32) / 32768.0
        audio = torch.from_numpy(samples)
        with self._lock:
            self._model.reset_states()
            with torch.no_grad():
                segs = self._fn(
                    audio, self._model, sampling_rate=16000,
                    threshold=self.threshold,
                    min_speech_duration_ms=self.min_speech_ms,
                    min_silence_duration_ms=100, speech_pad_ms=0,
                    return_seconds=False)
        return bool(segs)


def get_verifier(cfg) -> object:
    """Build the app-shared verifier.

    Disabled via FSCRIBE_SILERO_ENABLED=0 -> NullVerifier (accept all).
    Enabled (default) and the pinned Silero stack is missing/broken -> fail
    startup explicitly: a missing verifier must never silently reject audio.
    """
    if not getattr(cfg, "silero_enabled", False):
        return NullVerifier()
    try:
        import torch
    except Exception as exc:  # noqa: BLE001
        raise VerifierUnavailable(
            "silero verifier unavailable: torch import failed (%s)" % type(exc).__name__) from exc
    prev_threads = torch.get_num_threads()
    try:
        from silero_vad import get_speech_timestamps, load_silero_vad
        model = load_silero_vad()
    except Exception as exc:  # noqa: BLE001
        raise VerifierUnavailable(
            "silero verifier unavailable: %s" % type(exc).__name__) from exc
    finally:
        # silero_vad.model sets torch to one thread at import; restore the
        # process setting so the CPU aligner keeps its previous parallelism.
        try:
            torch.set_num_threads(prev_threads)
        except Exception:  # noqa: BLE001
            pass
    return SileroVerifier(model, get_speech_timestamps)
