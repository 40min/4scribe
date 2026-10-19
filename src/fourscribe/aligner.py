"""Internal forced aligner seam (plan 5: internal component of 4scribe).

Real implementation: Qwen/Qwen3-ForcedAligner-0.6B, bf16, CPU, loaded OFFLINE
from the preserved local snapshot (HF_HOME points at the existing read-only
eval cache; HF_HUB_OFFLINE=1 - no downloads, no cache writes). Lazy load on
first use (cold load ~0.5-3 s), executed on worker threads only.

Contract: `align()` returns word dicts [{word, start, end}] for the EXACT
final text against the SAME audio samples, times relative to the supplied
audio start. Times are validated (finite, 0 <= a <= b, ordered /
non-overlapping); on any violation an AlignmentFault is raised - never
fabricated or silently dropped times. If the torch stack is unavailable the
UnavailableAligner keeps finals explicit-fault degraded (words [] + fault
meta), per plan 5: never final-only as a normal path.
"""
import logging
import math
import os
import threading

import numpy as np

logger = logging.getLogger("fscribe")

LANG_NAMES = {"ru": "Russian", "en": "English"}


class AlignerUnavailable(RuntimeError):
    pass


class AlignmentFault(RuntimeError):
    pass


class Aligner:
    name: str = "aligner"
    available: bool = False

    def align(self, pcm: bytes, text: str, language: str) -> list:
        """Return [{word, start, end}] for the EXACT final text against the
        SAME audio samples. Must not fabricate."""
        raise NotImplementedError


def _validate_words(words):
    out = []
    prev_end = None
    for w in words:
        a, b = float(w["start"]), float(w["end"])
        if not (math.isfinite(a) and math.isfinite(b)) or a < 0 or b < a:
            raise AlignmentFault("invalid word interval")
        if prev_end is not None and a < prev_end - 1e-6:
            raise AlignmentFault("overlapping/unordered word times")
        out.append({"word": str(w["word"]), "start": round(a, 3), "end": round(b, 3)})
        prev_end = b
    return out


class UnavailableAligner:
    name = "unavailable"
    available = False
    reason = ("aligner stack unavailable (torch/transformers/qwen-asr not "
              "importable); word timestamps degraded per plan, fault explicit")

    def align(self, pcm: bytes, text: str, language: str) -> list:
        raise AlignerUnavailable(self.reason)


class QwenForcedAligner:
    """Offline Qwen3-ForcedAligner-0.6B (bf16 CPU), lazy single load.

    Deterministic weight resolution: the PRE-COMMITTED durable snapshot under
    the configured HF home is resolved DIRECTLY (durable cache layout
    <hf_home>/hub/models--<org>--<name>/snapshots/<rev> via refs/main) and
    passed to from_pretrained as a local directory. NO reliance on late
    HF_HOME env mutation: huggingface_hub.constants resolves its default
    cache at FIRST import, so env strings set afterwards do NOT redirect the
    library (the production-startup bug this class fixes - words[] finals
    because weights silently resolved to an empty default .cache dir).
    """

    name = "qwen3-forced-aligner-0.6b"
    available = True

    def __init__(self, model_id="Qwen/Qwen3-ForcedAligner-0.6B",
                 hf_home="/tmp/4scribe-backend-eval/hf", device="cpu"):
        self.model_id = model_id
        self.hf_home = hf_home
        self.device = device
        self._model = None
        self._lock = threading.Lock()

    @staticmethod
    def resolve_snapshot(hf_home: str, model_id: str = "Qwen/Qwen3-ForcedAligner-0.6B"):
        """Return the local snapshot directory, or None if not present."""
        import pathlib
        repo = "models--" + model_id.replace("/", "--")
        mdir = pathlib.Path(hf_home) / "hub" / repo
        if not mdir.is_dir():
            return None
        refs = mdir / "refs" / "main"
        if refs.is_file():
            rev = (refs.read_text().strip() or "").strip()
            snap = mdir / "snapshots" / rev
            if rev and snap.is_dir():
                return str(snap)
        # single snapshot fallback (only safely used when exactly one exists)
        snaps = list((mdir / "snapshots").glob("*")) if (mdir / "snapshots").is_dir() else []
        return str(snaps[0]) if len(snaps) == 1 else None

    def _ensure_model(self):
        with self._lock:
            if self._model is not None:
                return
            # defense-in-depth only: WITH the explicit snapshot path no cache
            # lookup happens at all; offline env just forbids accidents.
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
            os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
            import torch  # local import: keeps torch off the ASR-only paths
            from qwen_asr import Qwen3ForcedAligner
            snapshot = self.resolve_snapshot(self.hf_home, self.model_id)
            target = snapshot if snapshot else self.model_id
            if snapshot is None:
                logger.warning("aligner: no pre-committed snapshot under %s; "
                               "falling back to hub id %r", self.hf_home, self.model_id)
            try:
                self._model = Qwen3ForcedAligner.from_pretrained(
                    target, dtype=torch.bfloat16, device_map=self.device)
            except Exception as exc:  # noqa: BLE001 - sanitized diagnosability
                self._model = None
                detail = getattr(exc, "strerror", None) or ""
                path = getattr(exc, "filename", None)
                if not detail:
                    detail = str(exc)[:120]
                if path and not str(path).startswith(self.hf_home):
                    path = None  # never widen beyond the durable cache location
                raise AlignmentFault(
                    f"aligner model load failed: {type(exc).__name__}"
                    + (f"; detail {detail}" if detail else "")
                    + (f" under {path}" if path else "")) from exc
            logger.info("aligner loaded offline from %s", target or self.hf_home)

    def align(self, pcm: bytes, text: str, language: str) -> list:
        lang = LANG_NAMES.get((language or "").lower())
        if lang is None:
            raise AlignmentFault(f"unsupported aligner language {language!r}")
        self._ensure_model()
        arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if arr.size == 0:
            raise AlignmentFault("empty audio for alignment")
        try:
            res = self._model.align(audio=(arr, 16000), text=text, language=lang)
        except Exception as exc:  # noqa: BLE001 - surface as explicit fault
            raise AlignmentFault(f"aligner failure: {type(exc).__name__}") from exc
        if isinstance(res, list):
            res = res[0] if res else None
        if res is None or not getattr(res, "items", None):
            raise AlignmentFault("aligner returned no words")
        words = [{"word": it.text, "start": it.start_time, "end": it.end_time}
                 for it in res.items]
        return _validate_words(words)


def get_aligner(cfg=None) -> Aligner:
    """Single construction site. Falls back to the explicit-fault aligner when
    the torch stack is not importable (import attempted lazily, never at app
    construction time, so the service starts without the heavy deps)."""
    enabled = getattr(cfg, "aligner_enabled", True)
    if enabled:
        try:
            import torch  # noqa: F401
            import qwen_asr  # noqa: F401
            return QwenForcedAligner(
                model_id=getattr(cfg, "aligner_model", "Qwen/Qwen3-ForcedAligner-0.6B"),
                hf_home=getattr(cfg, "aligner_hf_home", "/tmp/4scribe-backend-eval/hf"),
                device=getattr(cfg, "aligner_device", "cpu"))
        except ImportError as exc:
            logger.warning("aligner stack unavailable: %s", exc)
    return UnavailableAligner()
