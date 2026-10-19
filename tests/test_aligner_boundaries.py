"""Word-boundary clamping and short-fixture partial-policy regressions."""
import asyncio
import concurrent.futures

import pytest

from fourscribe.aligner import (
    WORD_END_TOLERANCE_S, AlignmentFault, validate_words,
)
from fourscribe.asr import DecodeResult
from fourscribe.config import Config
from fourscribe.session import RealtimeSession
from fourscribe.verifier import NullVerifier
from tests.conftest import speech_pcm


def test_exact_segment_end_is_unchanged():
    words = [{"word": "a", "start": 0.10, "end": 1.00},
             {"word": "b", "start": 1.00, "end": 1.00}]
    out = validate_words(words, 1.00)
    assert out[0] == {"word": "a", "start": 0.1, "end": 1.0}
    assert out[1] == {"word": "b", "start": 1.0, "end": 1.0}


def test_small_end_overshoot_is_clamped_end_only():
    # 60 ms is the accepted short-fixture observation; 80 ms is one aligner frame.
    words = [{"word": "грезы", "start": 1.28, "end": 1.52}]
    out = validate_words(words, 1.46)
    assert out == [{"word": "грезы", "start": 1.28, "end": 1.46}]


def test_end_overshoot_at_tolerance_boundary_clamps():
    out = validate_words([{"word": "x", "start": 0.0,
                           "end": 1.0 + WORD_END_TOLERANCE_S}], 1.0)
    assert out[0]["end"] == 1.0


def test_gross_end_overshoot_is_fault():
    with pytest.raises(AlignmentFault, match="exceeds segment duration"):
        validate_words([{"word": "x", "start": 0.0,
                         "end": 1.0 + WORD_END_TOLERANCE_S + 0.001}], 1.0)


def test_start_after_segment_is_fault():
    with pytest.raises(AlignmentFault, match="start beyond"):
        validate_words([{"word": "x", "start": 1.01, "end": 1.02}], 1.0)


@pytest.mark.parametrize("words", [
    [{"word": "x", "start": -0.1, "end": 0.2}],
    [{"word": "x", "start": 0.2, "end": 0.1}],
    [{"word": "x", "start": float("nan"), "end": 0.2}],
    [{"word": "x", "start": 0.0, "end": float("inf")}],
    [{"word": "a", "start": 0.0, "end": 0.5},
     {"word": "b", "start": 0.4, "end": 0.6}],
])
def test_invalid_or_unordered_intervals_remain_faults(words):
    with pytest.raises(AlignmentFault):
        validate_words(words, 1.0)


def test_sequence_and_interior_times_are_preserved():
    words = [{"word": "один", "start": 0.05, "end": 0.31},
             {"word": "два", "start": 0.31, "end": 0.77},
             {"word": "три", "start": 0.77, "end": 1.04}]
    out = validate_words(words, 1.00)
    assert [w["word"] for w in out] == ["один", "два", "три"]
    assert out[0] == {"word": "один", "start": 0.05, "end": 0.31}
    assert out[1] == {"word": "два", "start": 0.31, "end": 0.77}
    assert out[2] == {"word": "три", "start": 0.77, "end": 1.0}
    assert out[0]["start"] != out[1]["start"] != out[2]["start"]


class _FakeASR:
    def decode(self, pcm, language):
        return DecodeResult(text="слово", prompt_tok=1, eval_tok=1, wall_s=0.0)


class _OvershootAligner:
    name = "overshoot-aligner"
    available = True

    def __init__(self, overshoot_s):
        self.overshoot_s = overshoot_s

    def align(self, pcm, text, language):
        dur = (len(pcm) // 2) / 16000
        return [{"word": "слово", "start": 0.0, "end": dur + self.overshoot_s}]


def _run_final(overshoot_s):
    sink = []
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        s = RealtimeSession(
            Config(silero_enabled=False), _FakeASR(),
            _OvershootAligner(overshoot_s), pool, sink.append,
            verifier=NullVerifier())
        s.language = "ru"
        s.word_timestamps_requested = True
        s.epoch_start_samples = 0
        pcm = speech_pcm(1.0)
        asyncio.run(s._finalize(pcm, 1.5 * 16000, 0.5 * 16000, False, "jid"))
    finally:
        pool.shutdown(wait=True)
    final = [e for e in sink
             if e["type"] == "conversation.item.input_audio_transcription.completed"]
    return final[0]


def test_session_clamps_before_absolute_offset():
    final = _run_final(0.060)
    assert final["audio_processed"] == 1.5
    assert final["words"] == [{"word": "слово", "start": 0.5, "end": 1.5}]
    assert final["alignment"]["status"] == "ok"


def test_session_gross_overshoot_is_alignment_fault_not_fatal():
    final = _run_final(WORD_END_TOLERANCE_S + 0.200)
    assert final["transcript"] == "слово"
    assert final["words"] == []
    assert final["alignment"]["status"] == "fault"
