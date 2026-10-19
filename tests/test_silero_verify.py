"""Focused tests for the shared pre-decode Silero verification seam.

These test ACTUAL behavior: rejected partial/final never reach ASR, rejected
commit still ACKs and the clock advances, a verifier fault is explicit and
fails open, accepted finals keep word timestamps, and two sessions sharing one
verifier serialize state reset. No real Silero model is loaded here; the
pinned real model is smoke-tested separately.
"""
import asyncio
import concurrent.futures
import json
import sys
import threading
import time
from contextlib import contextmanager

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"))

from fourscribe import app as appmod
from fourscribe.config import Config
from fourscribe.session import RealtimeSession
from fourscribe.verifier import VerifierUnavailable, get_verifier
from tests.conftest import FakeAligner, drain_until, open_session, speech_pcm


@contextmanager
def _session(client, **kw):
    """Wrap conftest.open_session (a plain generator) as a context manager."""
    gen = open_session(client, **kw)
    try:
        ws, upd = next(gen)
        yield ws, upd
    finally:
        gen.close()


class AcceptAllVerifier:
    name = "accept-all"
    enabled = True
    available = True

    def verify(self, pcm):
        return True


class RejectAllVerifier:
    name = "reject-all"
    enabled = True
    available = True

    def verify(self, pcm):
        return False


class FaultVerifier:
    name = "fault"
    enabled = True
    available = True

    def verify(self, pcm):
        raise RuntimeError("injected verifier fault")


class FirstRejectThenAccept:
    name = "first-reject"
    enabled = True
    available = True

    def __init__(self):
        self.first = True
        self.calls = 0

    def verify(self, pcm):
        self.calls += 1
        if self.first:
            self.first = False
            return False
        return True


class ConcurrencyDetectingVerifier:
    """Mimics SileroVerifier's lock; overlap here means state reset can race."""
    name = "concurrency-probe"
    enabled = True
    available = True

    def __init__(self, hold_s=0.05):
        self._lock = threading.Lock()
        self._hold_s = hold_s
        self.in_flight = 0
        self.overlap = False
        self.calls = 0

    def verify(self, pcm):
        with self._lock:
            self.calls += 1
            if self.in_flight > 0:
                self.overlap = True
            self.in_flight += 1
            time.sleep(self._hold_s)
            self.in_flight -= 1
        return True


def _cfg(**kw):
    base = dict(token="test-token", ollama_url="http://mock-ollama",
                min_endpointing_ms=800, partial_interval_s=1000.0)
    base.update(kw)
    return Config(**base)


def _commit(ws):
    ws.send_text(json.dumps({"type": "input_audio_buffer.commit"}))


def test_rejected_final_no_asr_commit_acks_and_clock_advances(client_factory, mock):
    v = FirstRejectThenAccept()
    client = client_factory(cfg=_cfg(), verifier=v)
    with _session(client, lang="ru", endpointing=800) as (ws, _):
        ws.send_bytes(speech_pcm(0.6))
        _commit(ws)
        got = drain_until(ws, {"input_audio_buffer.committed"})
        kinds = [e["type"] for e in got]
        assert "conversation.item.input_audio_transcription.completed" not in kinds
        assert mock.requests == [], "rejected final must not call ASR"
        assert kinds.count("input_audio_buffer.committed") >= 1

        ws.send_bytes(speech_pcm(0.6))
        _commit(ws)
        got2 = drain_until(ws, {"conversation.item.input_audio_transcription.completed",
                                "input_audio_buffer.committed"})
        finals = [e for e in got2
                  if e["type"] == "conversation.item.input_audio_transcription.completed"]
        acks = [e for e in got2 if e["type"] == "input_audio_buffer.committed"]
        assert finals, "accepted second phrase must emit a final"
        assert acks, "commit must ACK"
        assert finals[0]["audio_processed"] > 0, "epoch clock must continue advancing"
        assert len(mock.requests) == 1, "only the accepted final calls ASR"


def test_rejected_partial_never_calls_asr(client_factory, mock):
    client = client_factory(cfg=_cfg(partial_interval_s=0.1),
                            verifier=RejectAllVerifier())
    with _session(client, lang="ru", endpointing=800) as (ws, _):
        ws.send_bytes(speech_pcm(1.2))
        time.sleep(0.4)
        _commit(ws)
        got = drain_until(ws, {"input_audio_buffer.committed"})
        kinds = [e["type"] for e in got]
        assert "conversation.item.input_audio_transcription.delta" not in kinds
        assert "conversation.item.input_audio_transcription.completed" not in kinds
        assert mock.requests == [], "rejected partial must not call ASR"


def test_verifier_fault_is_server_side_only_and_fails_open(client_factory, mock):
    client = client_factory(cfg=_cfg(), verifier=FaultVerifier())
    with _session(client, lang="ru", endpointing=800) as (ws, _):
        ws.send_bytes(speech_pcm(0.6))
        _commit(ws)
        got = drain_until(ws, {"conversation.item.input_audio_transcription.completed",
                               "input_audio_buffer.committed"})
        assert not [e for e in got if e["type"] == "error"], \
            "a recoverable verifier fault must not send a fatal wire error"
        assert any(e["type"] == "conversation.item.input_audio_transcription.completed"
                   for e in got), "fault must fail OPEN: real audio is not deleted"
        assert any(e["type"] == "input_audio_buffer.committed" for e in got), \
            "commit must still ACK after a verifier fault"
        assert len(mock.requests) == 1


def test_accepted_final_keeps_word_timestamps(client_factory, mock):
    client = client_factory(cfg=_cfg(), verifier=AcceptAllVerifier())
    with _session(client, lang="ru", endpointing=800, word_timestamps=True) as (ws, _):
        ws.send_bytes(speech_pcm(0.6))
        _commit(ws)
        got = drain_until(ws, {"conversation.item.input_audio_transcription.completed"})
        final = [e for e in got
                 if e["type"] == "conversation.item.input_audio_transcription.completed"][0]
        assert final["words"], "accepted final must keep word timestamps"
        assert final["alignment"]["status"] == "ok"
        assert len(mock.requests) == 1


def test_env_default_enabled_and_off_switch(monkeypatch):
    monkeypatch.delenv("FSCRIBE_SILERO_ENABLED", raising=False)
    monkeypatch.setenv("FSCRIBE_TOKEN", "x")
    assert Config.from_env().silero_enabled is True
    monkeypatch.setenv("FSCRIBE_SILERO_ENABLED", "0")
    assert Config.from_env().silero_enabled is False


def test_verifier_unavailable_fails_explicit(monkeypatch):
    monkeypatch.setitem(sys.modules, "silero_vad", None)
    with pytest.raises(VerifierUnavailable):
        get_verifier(Config(silero_enabled=True))


def test_create_app_fails_when_enabled_verifier_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "silero_vad", None)
    with pytest.raises(VerifierUnavailable):
        appmod.create_app(Config(token="x", ollama_url="http://mock",
                                 silero_enabled=True), aligner=FakeAligner())


def test_shared_verifier_serializes_two_sessions():
    v = ConcurrencyDetectingVerifier()
    cfg = Config(silero_enabled=True)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    vad_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    try:
        s1 = RealtimeSession(cfg, None, FakeAligner(), pool, lambda e: None,
                             verifier=v, vad_pool=vad_pool)
        s2 = RealtimeSession(cfg, None, FakeAligner(), pool, lambda e: None,
                             verifier=v, vad_pool=vad_pool)

        async def run():
            pcm = b"\x00\x00" * 1600
            await asyncio.gather(
                s1._verify_speech(pcm, kind="final"),
                s2._verify_speech(pcm, kind="final"))

        asyncio.run(run())
        assert s1.verifier is s2.verifier
        assert v.calls == 2
        assert not v.overlap, "shared verifier state reset must not race across streams"
    finally:
        pool.shutdown(wait=True)
        vad_pool.shutdown(wait=True)
