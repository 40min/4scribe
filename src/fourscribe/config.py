"""Runtime configuration. Env-driven; the token default is a TEST-ONLY token.

Never read production credential env into this service; the deployed Bearer is
configured separately (see docs of the owner-approved deployment plan). No
secret values are logged.
"""
import os
from dataclasses import dataclass


def _int(name, default):
    raw = (os.environ.get(name) or "").strip()
    return int(raw) if raw.isdigit() else default


def _float(name, default):
    raw = (os.environ.get(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    host: str = "127.0.0.1"
    port: int = 8093
    token: str = "test-token"  # test-only default; set FSCRIBE_TOKEN for live use
    ollama_url: str = "http://127.0.0.1:11434"
    model: str = "frozenlab/qwen3-asr:1.7b"
    keep_alive_s: int = 300
    num_ctx: int = 8192
    num_predict: int = 2048
    inference_workers: int = 2
    sample_rate: int = 16000
    min_endpointing_ms: int = 800
    partial_interval_s: float = 4.0
    buffer_cap_s: float = 60.0
    language_options: tuple = ("ru", "en")
    aligner_enabled: bool = True
    aligner_model: str = "Qwen/Qwen3-ForcedAligner-0.6B"
    aligner_hf_home: str = "/tmp/4scribe-backend-eval/hf"
    aligner_device: str = "cpu"
    aligner_workers: int = 2
    max_pending_finals: int = 4
    outbound_queue_max: int = 512
    # Pre-decode Silero speech verification. Dataclass default is OFF so
    # explicit Config() construction (tests/embedding) is unchanged; the
    # env-driven production launch (Config.from_env) enables it by default.
    # Disable live with FSCRIBE_SILERO_ENABLED=0.
    silero_enabled: bool = False
    # Mobile batch API (HardAfanasii, POST /4scribe/v1/audio/transcriptions).
    # A SEPARATE Bearer that is never the realtime token. Empty default keeps
    # the endpoint disabled/fail-closed for existing deployments; the owner
    # provisions FSCRIBE_MOBILE_TOKEN in the external env file separately.
    # Missing, placeholder, too-short or WS-token-equal values disable batch
    # (503) instead of opening an unauthenticated or shared-key path.
    mobile_token: str = ""
    mobile_total_timeout_s: float = 90.0
    mobile_convert_timeout_s: float = 15.0
    mobile_max_duration_s: float = 61.0
    # ASR window stays well below the ~60 s Qwen context: 61 s client audio is
    # split at pauses into windows of at most this length (5..60 enforced).
    mobile_asr_window_s: float = 40.0
    mobile_ffmpeg_bin: str = "ffmpeg"
    # Standalone text enhancement (Loqui), POST /4scribe/v1/text/enhance.
    # Its Bearer is separate from both the realtime and mobile keys; empty or
    # unsafe values keep the endpoint disabled (503) instead of falling back.
    # The corrector itself is the behaviour-preserving qwen3:0.6b port.
    enhancement_token: str = ""
    enhancer_model: str = "qwen3:0.6b"
    enhancement_deadline_s: float = 5.0
    # Separate finite budget for the submitted Ollama call. The 5 s client
    # deadline can answer 504 while generation is still running; admission is
    # retained until this upstream call actually completes.
    enhancement_upstream_timeout_s: float = 60.0

    @classmethod
    def from_env(cls):
        return cls(
            port=_int("FSCRIBE_PORT", 8093),
            token=os.environ.get("FSCRIBE_TOKEN") or "test-token",
            ollama_url=os.environ.get("FSCRIBE_OLLAMA_URL") or "http://127.0.0.1:11434",
            model=os.environ.get("FSCRIBE_MODEL") or cls.model,
            keep_alive_s=_int("FSCRIBE_KEEP_ALIVE_S", 300),
            num_ctx=_int("FSCRIBE_NUM_CTX", 8192),
            inference_workers=_int("FSCRIBE_INFERENCE_WORKERS", 2),
            aligner_enabled=(os.environ.get("FSCRIBE_ALIGNER_ENABLED", "1") != "0"),
            aligner_model=os.environ.get("FSCRIBE_ALIGNER_MODEL") or cls.aligner_model,
            aligner_hf_home=os.environ.get("FSCRIBE_ALIGNER_HF_HOME") or cls.aligner_hf_home,
            aligner_workers=_int("FSCRIBE_ALIGNER_WORKERS", 2),
            max_pending_finals=_int("FSCRIBE_MAX_PENDING_FINALS", 4),
            outbound_queue_max=_int("FSCRIBE_OUTBOUND_QUEUE_MAX", 512),
            silero_enabled=(os.environ.get("FSCRIBE_SILERO_ENABLED", "1") != "0"),
            mobile_token=(os.environ.get("FSCRIBE_MOBILE_TOKEN") or "").strip(),
            mobile_total_timeout_s=_float("FSCRIBE_MOBILE_TOTAL_TIMEOUT_S", 90.0),
            mobile_convert_timeout_s=_float("FSCRIBE_MOBILE_CONVERT_TIMEOUT_S", 15.0),
            mobile_max_duration_s=_float("FSCRIBE_MOBILE_MAX_DURATION_S", 61.0),
            mobile_asr_window_s=_float("FSCRIBE_MOBILE_ASR_WINDOW_S", 40.0),
            mobile_ffmpeg_bin=os.environ.get("FSCRIBE_MOBILE_FFMPEG_BIN") or "ffmpeg",
            enhancement_token=(os.environ.get("FSCRIBE_ENHANCEMENT_TOKEN") or "").strip(),
            enhancer_model=(os.environ.get("FSCRIBE_ENHANCER_MODEL")
                            or "qwen3:0.6b"),
            enhancement_deadline_s=_float("FSCRIBE_ENHANCEMENT_DEADLINE_S", 5.0),
            enhancement_upstream_timeout_s=_float(
                "FSCRIBE_ENHANCEMENT_UPSTREAM_TIMEOUT_S", 60.0),
        )
