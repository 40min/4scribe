"""Cache-selection-after-imports regression (the production startup bug):
huggingface_hub resolves its default cache constants at first import, so a
late HF_HOME env mutation does NOT redirect weight lookup. The service must
therefore resolve the PRE-COMMITTED durable snapshot explicitly and load from
that local directory (no env reliance). Hermetic: real snapshot path
resolution + a stubbed from_pretrained; no real weight load in tests (the
real same-user OFFLINE check runs separately via
deployment/real_aligner_check.py).
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

import fourscribe.aligner as aligner_mod  # noqa: E402

DURABLE = "/opt/40zen/services/4scribe/model-cache/hf"
EXPECTED_SNAPSHOT = (
    f"{DURABLE}/hub/models--Qwen--Qwen3-ForcedAligner-0.6B/"
    "snapshots/c7cbfc2048c462b0d63a45797104fc9db3ad62b7")


def test_resolution_env_immune_and_prefers_precommitted_snapshot():
    # app-construction import order: qwen_asr/torch import the hub constants
    # FIRST; resolution must not depend on any env state at call time
    import qwen_asr  # noqa: F401  (warms huggingface_hub constants)
    import huggingface_hub.constants  # noqa: F401
    resolved = aligner_mod.QwenForcedAligner.resolve_snapshot(DURABLE)
    assert resolved == EXPECTED_SNAPSHOT

    # env-free: a decoy HF_HOME must not influence resolution
    os.environ["HF_HOME"] = "/tmp/decoy/no-weights"
    try:
        assert aligner_mod.QwenForcedAligner.resolve_snapshot(DURABLE) == EXPECTED_SNAPSHOT
    finally:
        os.environ.pop("HF_HOME", None)


def test_missing_cache_resolves_none(monkeypatch):
    monkeypatch.delenv("HF_HOME", raising=False)
    assert aligner_mod.QwenForcedAligner.resolve_snapshot(
        "/opt/40zen/services/4scribe/model-cache/does-not-exist") is None
    mono = aligner_mod.QwenForcedAligner(hf_home="also/missing")
    assert getattr(mono, "_model", None) is None


def test_ensure_model_targets_resolved_snapshot_without_env_writes(monkeypatch):
    recorded = {}

    class FakeAlignerCls:
        @classmethod
        def from_pretrained(cls, target, **kw):
            recorded["target"] = target
            recorded["kw"] = kw
            return object()

    os.environ.pop("HF_HOME", None)
    monkeypatch.delenv("HF_HOME", raising=False)
    # pre-warm hub constants exactly like the running app does
    import qwen_asr  # noqa: F401
    monkeypatch.setattr(qwen_asr, "Qwen3ForcedAligner", FakeAlignerCls, raising=False)
    al = aligner_mod.QwenForcedAligner(hf_home=DURABLE)
    al._ensure_model()
    assert recorded["target"] == EXPECTED_SNAPSHOT
    import torch
    assert recorded["kw"]["dtype"] == torch.bfloat16
    assert recorded["kw"]["device_map"] == "cpu"
    assert "HF_HOME" not in os.environ, "fix must not regress to late env mutation"
