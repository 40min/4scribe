"""Shared fixtures: mock Ollama transport + TestClient over the realtime app."""
import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tests.mock_ollama import MockOllama
from fourscribe.config import Config
from fourscribe import app as appmod

URL = "ws://testserver/v1/audio/transcriptions/realtime"


class FakeAligner:
    """Deterministic aligner stand-in: spreads the EXACT text words evenly
    (strictly ordered, non-overlapping) over the supplied audio duration."""

    name = "fake-aligner"
    available = True

    def align(self, pcm: bytes, text: str, language: str) -> list:
        dur = len(pcm) / (2 * 16000)
        words = text.split()
        if not words or dur <= 0:
            return []
        step = dur / (len(words) + 1)
        return [{"word": w, "start": round(step * i, 3), "end": round(step * (i + 1), 3)}
                for i, w in enumerate(words)]


@pytest.fixture
def mock():
    return MockOllama(delay_s=0.02)


@pytest.fixture
def client_factory(mock):
    def make(cfg=None, mock_delay=0.02, aligner="fake", freq_texts=None, verifier=None):
        mock.delay_s = mock_delay
        mock.freq_texts = freq_texts or {}
        cfg = cfg or Config(token="test-token", ollama_url="http://mock-ollama",
                            min_endpointing_ms=800)
        al = aligner
        if al == "fake":
            al = FakeAligner()
        elif al == "unavailable":
            from fourscribe.aligner import UnavailableAligner
            al = UnavailableAligner()
        return TestClient(appmod.create_app(cfg, lambda: httpx.Client(transport=mock.transport(), base_url=cfg.ollama_url), aligner=al, verifier=verifier))
    return make


@pytest.fixture
def client(client_factory):
    return client_factory()


def speech_pcm(seconds: float, rate=16000, amp=6000.0, freq=220.0) -> bytes:
    import array, math
    n = int(seconds * rate)
    out = array.array("h")
    for i in range(n):
        out.append(int(amp * math.sin(2 * math.pi * freq * i / rate)))
    return out.tobytes()


def silence_pcm(seconds: float, rate=16000) -> bytes:
    return b"\x00\x00" * int(seconds * rate)


def open_session(client, lang="ru", endpointing=800, word_timestamps=True,
                 token="test-token"):
    ctx = client.websocket_connect(URL, headers={"authorization": "Bearer " + token})
    try:
        ws = ctx.__enter__()
        upd = None
        if lang is not None:
            ws.send_text(json.dumps({"type": "session.update",
                                     "session": {"sample_rate": 16000, "language": lang,
                                                 "endpointing_ms": endpointing,
                                                 "word_timestamps": word_timestamps}}))
            upd = json.loads(ws.receive_text())
        yield ws, upd
    finally:
        ctx.__exit__(None, None, None)


def drain_until(ws, want_types, max_events=80):
    got = []
    for _ in range(max_events):
        ev = json.loads(ws.receive_text())
        got.append(ev)
        if all(any(e["type"] == t for e in got) for t in want_types):
            return got
    return got
