"""Hermetic regression exercising deployment/smoke_wss.py ITSELF against the
actual local mock service (same uvicorn/fakes as test_service_http): wire
contract fields the script asserts, run-to-ACK, ACK-required, frozen reader on
a non-talking server (cancellation safety), and strictness against the legacy
`word_timestamps` field name. Bounded; no external model work."""
import asyncio
import inspect
import json
import sys
import wave
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tests.conftest as conftest_mod  # speech_pcm helper lives in tests/
from tests.test_service_http import server_factory  # noqa: F401 - fixture reuse


def _load_smoke_module():
    path = Path(__file__).resolve().parents[1] / "deployment" / "smoke_wss.py"
    spec = inspect.importlib.util.spec_from_file_location("fs_deployment_smoke", path)
    mod = inspect.importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def smoke():
    return _load_smoke_module()


@pytest.fixture
def fixture_wav(tmp_path):
    pcm = conftest_mod.speech_pcm(1.2)
    p = tmp_path / "fixture.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm)
    return str(p)


def _make_local_snapshot_server(token: str):
    """Real uvicorn mock service with the LIBRARY tokenization aligner fake
    (raw-split FakeAligner in conftest remains the 29-test baseline pairing
    with the OLD verifier expectations and is intentionally unchanged)."""
    import httpx
    import threading
    import time
    import uvicorn
    from fourscribe import app as appmod
    from fourscribe.config import Config
    from tests.mock_ollama import MockOllama
    from tests.test_service_http import _free_port
    mock = MockOllama(delay_s=0.05)
    cfg = Config(token=token, ollama_url="http://mock-ollama",
                 min_endpointing_ms=400, partial_interval_s=0.2)
    app = appmod.create_app(
        cfg, lambda: httpx.Client(transport=mock.transport(), base_url=cfg.ollama_url),
        aligner=_LibraryAlignerFake())
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started
    return server, f"ws://127.0.0.1:{port}/4scribe/v1/audio/transcriptions/realtime"


def test_smoke_auth_session_passes_against_local_mock_service(
        smoke, fixture_wav):
    server, url = _make_local_snapshot_server("smoke-contract-token")
    try:
        ok = asyncio.run(smoke.auth_session(url, "smoke-contract-token", fixture_wav, "ru"))
        assert ok, "auth_session must accept a contract-correct local mock service"
    finally:
        server.should_exit = True


class _LibraryAlignerFake:
    """Aligner fake mirroring the REAL library tokenization rule: item words
    are the final-text tokens with punctuation stripped (case/times kept).
    (conftest.FakeAligner reproduces the raw-split contract for the existing
    29 baseline tests and is intentionally NOT modified)."""

    name = "library-fake-aligner"
    available = True

    def __init__(self):
        self.inner = conftest_mod.FakeAligner()

    def align(self, pcm, text, language):
        return [{"word": "".join(ch for ch in w["word"] if ch == "'" or ch.isalnum()),
                 "start": w["start"], "end": w["end"]}
                for w in self.inner.align(pcm, text, language)
                if any(ch == "'" or ch.isalnum() for ch in w["word"])]


class _NoDeltaServer:
    """Short-final-only wire: session.updated, final, ACK, no delta events."""

    def __init__(self):
        self.events = [
            {"type": "session.updated", "event_id": "u1",
             "session": {}, "applied": {"language": "ru", "words": True}},
            {"type": "conversation.item.input_audio_transcription.completed",
             "event_id": "f1", "transcript": "тест",
             "audio_processed": 1.2,
             "words": [{"word": "тест", "start": 0.1, "end": 0.3}],
             "alignment": {"status": "ok"}},
            {"type": "input_audio_buffer.committed", "event_id": "a1",
             "audio_processed": 0.0},
        ]

    async def recv(self):
        if self.events:
            return json.dumps(self.events.pop(0))
        await asyncio.Event().wait()

    async def send(self, _data):
        return None

    async def close(self):
        return None


def test_smoke_short_final_only_can_explicitly_allow_no_deltas(
        smoke, fixture_wav, monkeypatch):
    import websockets
    fake = _NoDeltaServer()
    monkeypatch.setattr(websockets, "connect", _fake_connect_factory(fake), raising=False)
    assert asyncio.run(smoke.auth_session(
        "ws://127.0.0.1:1/v1/audio/transcriptions/realtime", "tok",
        fixture_wav, "ru", require_partials=False))

    strict = _NoDeltaServer()
    monkeypatch.setattr(websockets, "connect", _fake_connect_factory(strict), raising=False)
    assert not asyncio.run(smoke.auth_session(
        "ws://127.0.0.1:1/v1/audio/transcriptions/realtime", "tok",
        fixture_wav, "ru")), "default smoke must still require partials"


def test_smoke_expect_reject_shapes_against_local_mock_service(server_factory, smoke):
    base, mock = server_factory(token="smoke-contract-token")
    url = f"ws://{base}/4scribe/v1/audio/transcriptions/realtime"
    async def run():
        ok = await smoke.expect_reject(url, "no-token", {})
        ok2 = await smoke.expect_reject(
            url, "default-test-token", {"authorization": "Bearer test-token"})
        return ok and ok2
    assert asyncio.run(run())


class _HangServer:
    """Returns ONE legacy-style session.updated under the WRONG field name
    (`word_timestamps`), then never responds - exercises: field-contract
    strictness, ACK-required, reader timeout cancellation path (no
    CancelledError escape), and no silent ACCEPT."""

    def __init__(self):
        self._after_update = asyncio.Event()

    async def recv(self):
        if not self._after_update.is_set():
            self._after_update.set()
            return json.dumps({
                "type": "session.updated", "event_id": "f1",
                "session": {}, "applied": {"language": "ru", "word_timestamps": True},
            })
        await asyncio.Event().wait()  # hang until cancelled

    async def send(self, _data):
        return None

    async def close(self):
        return None


def test_smoke_rejects_legacy_field_and_missing_ack_no_cancelled_escape(
        server_factory, smoke, fixture_wav, monkeypatch):
    url = "ws://127.0.0.1:1/v1/audio/transcriptions/realtime"  # never contacted
    hang = _HangServer()
    import websockets
    monkeypatch.setattr(websockets, "connect", _fake_connect_factory(hang), raising=False)
    result_frame = {}

    async def run():
        ok = await smoke.auth_session(url, "whatever-token", fixture_wav,
                                      "ru", ack_timeout_s=0.5)
        result_frame["ok"] = ok
    asyncio.run(run())
    assert result_frame["ok"] is False, "legacy field name + missing ACK must FAIL"


def test_smoke_words_ok_accepts_library_tokenization_and_rejects_corruption(smoke):
    """The aligner library strips punctuation from item words (letters/
    digits/apostrophes, case preserved). The verifier must accept exactly
    that and still fail missing/reordered/extra/out-of-bounds words."""
    ap = 3.2
    good = [{"word": "Долго", "start": 0.1, "end": 0.3},
            {"word": "ждать", "start": 0.3, "end": 0.6}]
    assert smoke._words_ok(good, "Долго, ждать.", ap) == ""
    bad = [{"word": "Долго", "start": 0.1, "end": 0.3}]
    assert "does not match" in smoke._words_ok(bad, "Долго, ждать.", ap)
    reordered = [{"word": "ждать", "start": 0.1, "end": 0.3},
                 {"word": "Долго", "start": 0.3, "end": 0.6}]
    assert "does not match" in smoke._words_ok(reordered, "Долго, ждать.", ap)
    extra = good + [{"word": "больше", "start": 0.6, "end": 0.9}]
    assert "does not match" in smoke._words_ok(extra, "Долго, ждать.", ap)
    oob = [{"word": "Долго", "start": 2.9, "end": 6.0},
           {"word": "ждать", "start": 0.3, "end": 0.6}]
    assert "exceeds" in smoke._words_ok(oob, "Долго ждать", ap)


def _fake_connect_factory(fake_ws):
    # signature must expose the header kw arg the smoke's compatibility
    # helper looks for (mirrors the installed websockets.connect)
    async def fake_connect(url, additional_headers=None, open_timeout=None, **kw):
        return fake_ws  # websockets.connect is awaited to a usable client object
    return fake_connect
