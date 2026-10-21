"""Mobile batch API (HardAfanasii) contract + safety tests.

Hermetic: mock Ollama over httpx.MockTransport, fake aligner, deterministic
converter stubs, real subprocesses only for the bounded-exec helper (python
itself - no ffmpeg needed). The one real-ffmpeg test is skipped unless ffmpeg
is installed in the image (owner Docker smoke).

Covers the Kotlin client contract literally: separate mobile Bearer, required
numeric helper fields (enhancer_ms must be numeric 0, never null), auto/ru/en
language handling, empty no-speech success, M4A/WAV allowlist, bounded
multipart uploads without Content-Length, the 61 s duration gate, one-active
request admission, cancellation/deadline slot retention, and the shared
executor/decode-semaphore/verifier seams used by live WSS traffic.
"""
import asyncio
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fourscribe import app as appmod
from fourscribe import batch as batchmod
from fourscribe.config import Config
from tests.conftest import FakeAligner, speech_pcm
from tests.mock_ollama import MockOllama

if not batchmod._MULTIPART_AVAILABLE:  # pragma: no cover - deployment state
    pytest.skip("python-multipart missing: rebuild the image from "
                "requirements-container.txt before running batch tests",
                allow_module_level=True)

MOBILE_TOKEN = "mobile-batch-0000000000"
WS_TOKEN = "ws-realtime-0000000000"
PATH = "/4scribe/v1/audio/transcriptions"
LOOPBACK_PATH = "/v1/audio/transcriptions"
AUTH = {"authorization": "Bearer " + MOBILE_TOKEN}


# --------------------------------------------------------------- helpers ----

def wav_bytes(pcm, rate=16000):
    import io
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def audio_files(seconds=0.6, name="recording.wav", ctype="audio/wav"):
    return {"file": (name, wav_bytes(speech_pcm(seconds)), ctype)}


def parse_like_kotlin(payload):
    """Replica of ZenSttProtocol's response parser (required field types).

    text:String, enhanced_text:String?, enhancer_error:Boolean, and
    conversion_ms/stt_ms/enhancer_ms/server_total_ms as finite non-negative
    Numbers (null is a hard failure). Enhanced text wins only when filled and
    error-free; otherwise the raw text is used.
    """
    assert isinstance(payload, dict)
    text = payload["text"]
    assert isinstance(text, str)
    enhanced = payload["enhanced_text"]
    assert enhanced is None or isinstance(enhanced, str)
    assert payload["enhancer_error"] is False
    for field in ("conversion_ms", "stt_ms", "enhancer_ms", "server_total_ms"):
        value = payload[field]
        assert isinstance(value, (int, float)) and not isinstance(value, bool), field
        assert math.isfinite(float(value)), field
        assert value >= 0, field
    if enhanced is not None and not payload["enhancer_error"] and enhanced.strip():
        return enhanced
    return text if text is not None else ""


def fake_converter(pcm=None, seconds=1.0, ignore_cap=False, expect=None, calls=None):
    def convert(path, container, max_bytes, timeout_s, cancel_event=None,
                binary="ffmpeg"):
        data = Path(path).read_bytes()
        assert data, "uploaded bytes must be spooled before conversion"
        assert os.stat(path).st_mode & 0o077 == 0, "spooled upload must be private"
        if expect is not None:
            assert container == expect, f"unexpected demuxer {container!r}"
        if calls is not None:
            calls.append(container)
        out = pcm if pcm is not None else speech_pcm(seconds)
        if len(out) > max_bytes and not ignore_cap:
            raise batchmod.AudioTooLongError()
        return out
    return convert


def blocking_converter(started, gate, pcm):
    def convert(path, container, max_bytes, timeout_s, cancel_event=None,
                binary="ffmpeg"):
        started.set()
        if not gate.wait(15):
            raise batchmod.AudioConversionTimeout()
        return pcm
    return convert


class EmptyOllama(MockOllama):
    """Completed ASR response with the official no-speech marker."""

    def chat(self, body):
        self.requests.append(body)
        return httpx.Response(200, json={
            "message": {"content": "language None<asr_text>"},
            "done": True, "prompt_eval_count": 1, "eval_count": 0})


class BlockingOllama(MockOllama):
    """ASR that blocks in the worker thread until `release` is set."""

    def __init__(self, started, release, text="batched text"):
        super().__init__(delay_s=0.0)
        self._started = started
        self._release = release
        self._text = text
        self.finished = threading.Event()

    def chat(self, body):
        self.requests.append(body)
        self._started.set()
        try:
            self._release.wait(15)
        finally:
            self.finished.set()
        return httpx.Response(200, json={
            "message": {"content": self._text}, "done": True,
            "prompt_eval_count": 1, "eval_count": 1})


class DeclineVerifier:
    name = "decline-verifier"
    enabled = True
    available = True

    def verify(self, pcm):
        return False


class BlockingVerifier:
    """Silero stand-in that blocks in its worker thread until released."""

    name = "blocking-verifier"
    enabled = True
    available = True

    def __init__(self, started, release, result=True):
        self._started = started
        self._release = release
        self._result = result
        self.finished = threading.Event()

    def verify(self, pcm):
        self._started.set()
        try:
            self._release.wait(15)
        finally:
            self.finished.set()
        return self._result


async def _semaphore_is_held(sem, timeout=0.05):
    """True when no permit is available (capacity fully held by in-flight work)."""
    try:
        await asyncio.wait_for(sem.acquire(), timeout)
    except TimeoutError:
        return True
    sem.release()
    return False


def make_app(monkeypatch, cfg=None, mock=None, verifier=None, converter=None):
    cfg = cfg or Config(token=WS_TOKEN, mobile_token=MOBILE_TOKEN,
                        ollama_url="http://mock-ollama",
                        min_endpointing_ms=400, partial_interval_s=0.2)
    mock = mock or MockOllama(delay_s=0.0)
    if converter is not None:
        monkeypatch.setattr(batchmod, "decode_with_ffmpeg", converter)
    app = appmod.create_app(
        cfg,
        lambda: httpx.Client(transport=mock.transport(), base_url=cfg.ollama_url),
        aligner=FakeAligner(), verifier=verifier)
    return app, mock, cfg


def async_client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://testserver")


async def _wait_event(event, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not event.is_set() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert event.is_set(), "timed out waiting for the in-flight stage"


def _wait_thread(event, timeout=5.0):
    assert event.wait(timeout), "timed out waiting for worker completion"


# ------------------------------------------------------------ auth tests ----

def test_mobile_auth_is_separate_and_returns_401(monkeypatch):
    app, mock, _ = make_app(monkeypatch, converter=fake_converter())
    with TestClient(app) as client:
        assert client.post(PATH, files=audio_files()).status_code == 401
        assert client.post(PATH, headers={"authorization": "Bearer nope"},
                           files=audio_files()).status_code == 401
        # The realtime token must never authenticate the batch endpoint.
        assert client.post(PATH, headers={"authorization": "Bearer " + WS_TOKEN},
                           files=audio_files()).status_code == 401
        assert mock.requests == []
        ok = client.post(PATH, headers=AUTH, files=audio_files())
    assert ok.status_code == 200


def test_mobile_token_is_not_accepted_by_realtime_ws(monkeypatch):
    app, _, _ = make_app(monkeypatch, converter=fake_converter())
    with TestClient(app) as client:
        try:
            with client.websocket_connect(
                    "/v1/audio/transcriptions/realtime",
                    headers={"authorization": "Bearer " + MOBILE_TOKEN}) as ws:
                ws.receive_text()
        except WebSocketDisconnect as exc:
            assert exc.code == 1008
        else:
            pytest.fail("mobile token authenticated the realtime WS")


@pytest.mark.parametrize("token", ["", "test-token", "short", WS_TOKEN])
def test_disabled_configuration_fails_closed(monkeypatch, token):
    cfg = Config(token=WS_TOKEN, mobile_token=token,
                 ollama_url="http://mock-ollama")
    app, mock, _ = make_app(monkeypatch, cfg=cfg, converter=fake_converter())
    with TestClient(app) as client:
        resp = client.post(PATH, headers=AUTH, files=audio_files())
        assert resp.status_code == 503
        assert resp.json()["error"] == "batch_unconfigured"
        assert mock.requests == []
        health = client.get("/healthz").json()
    assert health["mobile_batch"]["enabled"] is False
    assert health["mobile_batch"]["reason"] in (
        "unset", "placeholder", "too_short", "shared_with_realtime_token")


def test_ws_startup_remains_valid_without_mobile_token(monkeypatch):
    cfg = Config(token=WS_TOKEN, ollama_url="http://mock-ollama")
    app, _, _ = make_app(monkeypatch, cfg=cfg, converter=fake_converter())
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").json()["workers"] == 2
        with client.websocket_connect(
                "/v1/audio/transcriptions/realtime",
                headers={"authorization": "Bearer " + WS_TOKEN}) as ws:
            ws.send_text(json.dumps({"type": "session.update", "session": {
                "sample_rate": 16000, "language": "ru", "endpointing_ms": 400,
                "word_timestamps": True}}))
            assert json.loads(ws.receive_text())["type"] == "session.updated"


# -------------------------------------------------------- contract tests ----

def test_auto_language_contract_types_and_no_prefill(monkeypatch):
    app, mock, _ = make_app(monkeypatch, converter=fake_converter())
    with TestClient(app) as client:
        resp = client.post(PATH, headers=AUTH, files=audio_files())
    assert resp.status_code == 200
    payload = resp.json()
    assert parse_like_kotlin(payload) == "Unforced test text."
    assert payload["language"] == "auto"
    assert payload["enhanced_text"] is None
    assert payload["enhancer_error"] is False
    assert payload["enhancer_ms"] == 0
    assert payload["audio_duration_ms"] > 0
    # Omitted language == current Android behaviour: no assistant prefill.
    assert all(m.get("role") != "assistant"
               for m in mock.requests[-1]["messages"])


@pytest.mark.parametrize("language,prefill,expected", [
    ("ru", "language Russian<asr_text>", "Финальная гипотеза."),
    ("en", "language English<asr_text>", "Forced final hypothesis."),
])
def test_forced_language_uses_existing_prefill(monkeypatch, language, prefill,
                                              expected):
    app, mock, _ = make_app(monkeypatch, converter=fake_converter())
    with TestClient(app) as client:
        resp = client.post(PATH, headers=AUTH,
                           files={**audio_files(), "language": (None, language)})
    assert resp.status_code == 200
    assert parse_like_kotlin(resp.json()) == expected
    assert resp.json()["language"] == language
    assert mock.requests[-1]["messages"][-1] == {
        "role": "assistant", "content": prefill}


@pytest.mark.parametrize("language", ["de", "auto-ish", "ru-RU"])
def test_invalid_language_rejected(monkeypatch, language):
    app, mock, _ = make_app(monkeypatch, converter=fake_converter())
    with TestClient(app) as client:
        resp = client.post(PATH, headers=AUTH,
                           files={**audio_files(), "language": (None, language)})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_language"
    assert mock.requests == []


def test_empty_no_speech_is_valid_empty_text(monkeypatch):
    app, mock, _ = make_app(monkeypatch, mock=EmptyOllama(),
                            converter=fake_converter())
    with TestClient(app) as client:
        resp = client.post(PATH, headers=AUTH, files=audio_files())
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["text"] == ""
    assert parse_like_kotlin(payload) == ""
    assert payload["stt_ms"] >= 0
    assert payload["enhancer_ms"] == 0
    assert len(mock.requests) == 1


def test_verifier_decline_returns_empty_without_asr(monkeypatch):
    app, mock, _ = make_app(monkeypatch, converter=fake_converter(),
                            verifier=DeclineVerifier())
    with TestClient(app) as client:
        resp = client.post(PATH, headers=AUTH, files=audio_files())
    assert resp.status_code == 200
    assert resp.json()["text"] == ""
    assert resp.json()["stt_ms"] == 0
    assert mock.requests == []


def test_m4a_upload_uses_allowlisted_demuxer(monkeypatch):
    calls = []
    m4a = b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00"
    app, mock, _ = make_app(monkeypatch,
                            converter=fake_converter(expect="mov", calls=calls))
    with TestClient(app) as client:
        resp = client.post(PATH, headers=AUTH,
                           files={"file": ("recording.m4a", m4a, "audio/mp4")})
    assert resp.status_code == 200
    assert calls == ["mov"]
    assert parse_like_kotlin(resp.json()) == "Unforced test text."


@pytest.mark.parametrize("ctype,filename", [
    ("video/mp4", "recording.mp4"),
    ("image/png", "recording.png"),
    ("application/zip", "recording.zip"),
])
def test_unsupported_format_rejected_before_conversion(monkeypatch, ctype,
                                                       filename):
    app, mock, _ = make_app(monkeypatch, converter=fake_converter())
    with TestClient(app) as client:
        resp = client.post(PATH, headers=AUTH,
                           files={"file": (filename, b"not-audio", ctype)})
    assert resp.status_code == 400
    assert resp.json()["error"] == "unsupported_audio_format"
    assert mock.requests == []


def test_octet_stream_falls_back_to_allowlisted_extension(monkeypatch):
    app, mock, _ = make_app(monkeypatch, converter=fake_converter(expect="wav"))
    with TestClient(app) as client:
        resp = client.post(PATH, headers=AUTH,
                           files={"file": ("recording.wav", wav_bytes(
                               speech_pcm(0.5)), "application/octet-stream")})
    assert resp.status_code == 200


def test_conversion_failure_maps_to_400(monkeypatch):
    def broken(*args, **kwargs):
        raise batchmod.AudioDecodeError("decoder exited 1")

    app, mock, _ = make_app(monkeypatch, converter=broken)
    with TestClient(app) as client:
        resp = client.post(PATH, headers=AUTH, files=audio_files())
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_audio"
    assert mock.requests == []


def test_asr_failure_maps_to_502(monkeypatch):
    class FailingOllama(MockOllama):
        def chat(self, body):
            self.requests.append(body)
            return httpx.Response(500, json={"error": "boom"})

    app, mock, _ = make_app(monkeypatch, mock=FailingOllama(),
                            converter=fake_converter())
    with TestClient(app) as client:
        resp = client.post(PATH, headers=AUTH, files=audio_files())
    assert resp.status_code == 502
    assert resp.json()["error"] == "asr_unavailable"


def test_legacy_enhance_flag_is_ignored_without_llm_call(monkeypatch):
    app, mock, _ = make_app(monkeypatch, converter=fake_converter())
    with TestClient(app) as client:
        resp = client.post(PATH + "?enhance=true", headers=AUTH,
                           files={**audio_files(), "enhance": (None, "true")})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["enhanced_text"] is None
    assert payload["enhancer_error"] is False
    assert payload["enhancer_ms"] == 0
    assert len(mock.requests) == 1, "no enhancer/extra model call allowed"


def test_loopback_alias_answers_like_prefixed_route(monkeypatch):
    app, _, _ = make_app(monkeypatch, converter=fake_converter())
    with TestClient(app) as client:
        prefixed = client.post(PATH, headers=AUTH, files=audio_files())
        alias = client.post(LOOPBACK_PATH, headers=AUTH, files=audio_files())
    assert prefixed.status_code == alias.status_code == 200
    assert prefixed.json()["text"] == alias.json()["text"]


# ------------------------------------------------------- upload bounds ------

class StubStream:
    """Minimal Request stand-in for the bounded body reader (no ASGI stack)."""

    def __init__(self, chunks, headers=None):
        self.headers = headers or {}
        self._chunks = chunks

    async def stream(self):
        for chunk in self._chunks:
            yield chunk


def test_read_bounded_body_counts_stream_without_content_length():
    stub = StubStream([b"a" * 1000, b"b" * (batchmod.MAX_BODY_BYTES + 1)])
    assert asyncio.run(batchmod.read_bounded_body(stub)) is None


def test_read_bounded_body_rejects_declared_oversize_early():
    stub = StubStream([], headers={
        "content-length": str(batchmod.MAX_BODY_BYTES + 1)})
    assert asyncio.run(batchmod.read_bounded_body(stub)) is None


def test_read_bounded_body_ignores_understated_content_length():
    stub = StubStream([b"a" * 2048], headers={"content-length": "10"})
    assert asyncio.run(batchmod.read_bounded_body(stub)) == b"a" * 2048


def test_oversized_chunked_upload_is_413_before_asr(monkeypatch):
    app, mock, _ = make_app(monkeypatch, converter=fake_converter())
    payload = b"x" * (batchmod.MAX_BODY_BYTES + 4096)
    headers = {"authorization": "Bearer " + MOBILE_TOKEN,
               "content-type": "multipart/form-data; boundary=batchboundary"}
    with TestClient(app) as client:
        resp = client.post(PATH, headers=headers, content=iter([payload]))
    assert resp.status_code == 413
    assert resp.json()["error"] == "payload_too_large"
    assert mock.requests == []


def test_file_over_2mib_is_413_before_conversion(monkeypatch):
    app, mock, _ = make_app(monkeypatch, converter=fake_converter())
    with TestClient(app) as client:
        resp = client.post(PATH, headers=AUTH, files={
            "file": ("recording.wav", b"\x00" * (batchmod.MAX_FILE_BYTES + 1),
                     "audio/wav")})
    assert resp.status_code == 413
    assert resp.json()["error"] == "payload_too_large"
    assert mock.requests == []


def test_missing_file_part_rejected(monkeypatch):
    app, mock, _ = make_app(monkeypatch, converter=fake_converter())
    with TestClient(app) as client:
        resp = client.post(PATH, headers=AUTH,
                           files={"audio": ("recording.wav", b"x", "audio/wav")})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"


# ------------------------------------------------------- 61 s gate ----------

@pytest.mark.parametrize("seconds,status", [(60.0, 200), (61.0, 200), (62.0, 413)])
def test_duration_gate_on_decoded_pcm(monkeypatch, seconds, status):
    # The converter ignores its output cap here so the handler's own decoded
    # duration check (not the subprocess cap) is what gets exercised.
    app, mock, _ = make_app(
        monkeypatch, converter=fake_converter(pcm=speech_pcm(seconds),
                                              ignore_cap=True))
    with TestClient(app) as client:
        resp = client.post(PATH, headers=AUTH,
                           files={"file": ("recording.m4a", b"m4a", "audio/mp4")})
    assert resp.status_code == status
    if status == 200:
        assert resp.json()["audio_duration_ms"] >= 60_000
        assert resp.json()["text"]
    else:
        assert resp.json()["error"] == "audio_too_long"


def test_plan_chunks_tiles_every_sample_within_window():
    pcm = speech_pcm(61.0)
    chunks = batchmod.plan_chunks(pcm, 40.0)
    assert len(chunks) >= 2
    assert chunks[0][0] == 0
    assert chunks[-1][1] == len(pcm) // 2
    for (begin, end), (next_begin, _) in zip(chunks, chunks[1:]):
        assert end == next_begin, "chunks must be contiguous, no gaps/overlap"
    assert all(0 < end - begin <= 40 * 16000 for begin, end in chunks)
    rebuilt = b"".join(pcm[begin * 2:end * 2] for begin, end in chunks)
    assert rebuilt == pcm, "no decoded tail may be dropped"


def test_plan_chunks_handles_silence_and_short_audio():
    silence = b"\x00\x00" * (61 * 16000)
    chunks = batchmod.plan_chunks(silence, 40.0)
    assert sum(end - begin for begin, end in chunks) == 61 * 16000
    assert all(end - begin <= 40 * 16000 for begin, end in chunks)
    assert batchmod.plan_chunks(speech_pcm(1.0), 40.0) == [(0, 16000)]


def test_stitch_is_verbatim_join():
    assert batchmod.stitch_texts([" a b ", "", "  ", "c"]) == "a b c"
    assert batchmod.stitch_texts([]) == ""


# ------------------------------------------------- admission / lifecycle ----

def test_batch_reuses_shared_executor_semaphore_and_verifier(monkeypatch):
    app, _, _ = make_app(monkeypatch, converter=fake_converter())
    transcriber = app.state.batch_transcriber
    assert transcriber.pool is app.state.decode_pool
    assert transcriber.decode_sem is app.state.decode_sem
    assert transcriber.verifier is app.state.verifier
    with TestClient(app) as client:
        health = client.get("/healthz").json()
    assert health["mobile_batch"] == {"enabled": True, "reason": "ok"}


def test_busy_second_upload_is_503_not_queued(monkeypatch):
    started, gate = threading.Event(), threading.Event()
    app, _, _ = make_app(monkeypatch, converter=blocking_converter(
        started, gate, speech_pcm(0.5)))

    async def scenario():
        async with async_client(app) as client:
            first = asyncio.create_task(client.post(PATH, headers=AUTH,
                                                    files=audio_files()))
            await _wait_event(started)
            second = await client.post(PATH, headers=AUTH, files=audio_files())
            gate.set()
            first_resp = await asyncio.wait_for(first, 10)
            return first_resp, second

    first_resp, second = asyncio.run(scenario())
    assert first_resp.status_code == 200
    assert second.status_code == 503
    assert second.json()["error"] == "busy"


def test_cancellation_keeps_slot_until_inflight_asr_finishes(monkeypatch):
    started, release = threading.Event(), threading.Event()
    mock = BlockingOllama(started, release)
    app, _, _ = make_app(monkeypatch, mock=mock,
                         converter=fake_converter(pcm=speech_pcm(0.5)))

    async def scenario():
        async with async_client(app) as client:
            task = asyncio.create_task(client.post(PATH, headers=AUTH,
                                                   files=audio_files()))
            await _wait_event(started)
            task.cancel()
            await asyncio.sleep(0.1)
            busy = await client.post(PATH, headers=AUTH, files=audio_files())
            still_running = not mock.finished.is_set()
            release.set()
            _wait_thread(mock.finished)
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 10)
            after = await client.post(PATH, headers=AUTH, files=audio_files())
            return busy, still_running, after

    busy, still_running, after = asyncio.run(scenario())
    assert busy.status_code == 503
    assert still_running, "slot must not be released while native ASR runs"
    assert after.status_code == 200


def test_slow_upload_is_bounded_by_total_deadline_and_releases_slot(monkeypatch):
    cfg = Config(token=WS_TOKEN, mobile_token=MOBILE_TOKEN,
                 ollama_url="http://mock-ollama", mobile_total_timeout_s=0.5)
    app, mock, _ = make_app(monkeypatch, cfg=cfg, converter=fake_converter())

    async def drip():
        # Authenticated chunked upload that never finishes on its own: the
        # body read itself must be bounded by the remaining total deadline.
        for _ in range(12):
            await asyncio.sleep(0.25)
            yield b"x" * 64

    async def scenario():
        async with async_client(app) as client:
            headers = dict(AUTH)
            headers["content-type"] = "multipart/form-data; boundary=dripboundary"
            began = time.monotonic()
            resp = await client.post(PATH, headers=headers, content=drip())
            elapsed = time.monotonic() - began
            after = await client.post(PATH, headers=AUTH, files=audio_files())
            return resp, elapsed, after

    resp, elapsed, after = asyncio.run(scenario())
    assert resp.status_code == 504
    assert resp.json()["error"] == "deadline_exceeded"
    assert elapsed < 5, "slow upload must be cut off by the deadline"
    assert after.status_code == 200, "admission guard must be released"
    assert mock.requests, "the follow-up request must reach ASR"


def test_verifier_cancellation_holds_capacity_until_native_completion(monkeypatch):
    started, release = threading.Event(), threading.Event()
    verifier = BlockingVerifier(started, release)
    cfg = Config(token=WS_TOKEN, mobile_token=MOBILE_TOKEN,
                 ollama_url="http://mock-ollama", inference_workers=1)
    app, _, _ = make_app(monkeypatch, cfg=cfg, verifier=verifier,
                         converter=fake_converter(pcm=speech_pcm(0.5)))
    sem = app.state.decode_sem

    async def scenario():
        async with async_client(app) as client:
            task = asyncio.create_task(client.post(PATH, headers=AUTH,
                                                   files=audio_files()))
            await _wait_event(started)
            busy = await client.post(PATH, headers=AUTH, files=audio_files())
            held_before = await _semaphore_is_held(sem)
            task.cancel()
            await asyncio.sleep(0.1)
            still_running = not verifier.finished.is_set()
            held_after = await _semaphore_is_held(sem)
            release.set()
            _wait_thread(verifier.finished)
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 10)
            freed = not await _semaphore_is_held(sem)
            after = await client.post(PATH, headers=AUTH, files=audio_files())
            return busy, held_before, still_running, held_after, freed, after

    busy, held_before, still_running, held_after, freed, after = asyncio.run(scenario())
    assert busy.status_code == 503
    assert held_before, "decode capacity must be held while Silero runs"
    assert still_running, "verifier thread must not be treated as cancelled"
    assert held_after, "cancellation must not release capacity before completion"
    assert freed, "capacity must be released after the verifier really ends"
    assert after.status_code == 200


def test_verifier_timeout_returns_504_after_native_completion(monkeypatch):
    started, release = threading.Event(), threading.Event()
    verifier = BlockingVerifier(started, release)
    cfg = Config(token=WS_TOKEN, mobile_token=MOBILE_TOKEN,
                 ollama_url="http://mock-ollama", inference_workers=1,
                 mobile_total_timeout_s=0.5)
    app, _, _ = make_app(monkeypatch, cfg=cfg, verifier=verifier,
                         converter=fake_converter(pcm=speech_pcm(0.5)))
    sem = app.state.decode_sem

    async def scenario():
        async with async_client(app) as client:
            task = asyncio.create_task(client.post(PATH, headers=AUTH,
                                                   files=audio_files()))
            await _wait_event(started)
            await asyncio.sleep(1.3)  # past the (clamped) 1 s deadline
            busy = await client.post(PATH, headers=AUTH, files=audio_files())
            held = await _semaphore_is_held(sem)
            still_running = not verifier.finished.is_set()
            release.set()
            slow = await asyncio.wait_for(task, 10)
            after = await client.post(PATH, headers=AUTH, files=audio_files())
            return slow, busy, held, still_running, after

    slow, busy, held, still_running, after = asyncio.run(scenario())
    assert slow.status_code == 504
    assert slow.json()["error"] == "deadline_exceeded"
    assert busy.status_code == 503
    assert held, "decode capacity must stay held across the verifier timeout"
    assert still_running, "the 504 must wait for native verification to finish"
    assert after.status_code == 200


def test_form_parts_are_closed_on_every_path(monkeypatch):
    snapshots = []
    real_close = batchmod.close_form

    def spy(form):
        # handle()'s finally legitimately calls close_form(None); preserve the
        # wrapped helper's no-op semantics and record only actual forms.
        if form is None:
            real_close(form)
            return
        uploads = [value for _key, value in form.multi_items()
                   if not isinstance(value, str)]
        real_close(form)
        snapshots.append([
            (getattr(getattr(value, "file", None), "closed", False),
             getattr(value, "filename", None))
            for value in uploads])

    monkeypatch.setattr(batchmod, "close_form", spy)
    app, mock, _ = make_app(monkeypatch, converter=fake_converter())
    with TestClient(app) as client:
        multi = client.post(PATH, headers=AUTH, files=[
            ("file", ("a.wav", b"aaaa", "audio/wav")),
            ("file", ("b.wav", b"bbbb", "audio/wav")),
        ])
        wrong_name = client.post(PATH, headers=AUTH, files={
            "audio": ("c.wav", b"cccc", "audio/wav")})
        ok = client.post(PATH, headers=AUTH, files=audio_files())

    assert multi.status_code == 400
    assert multi.json()["error"] == "invalid_request"
    assert wrong_name.status_code == 400
    assert ok.status_code == 200
    assert len(snapshots) == 3, "each parsed form must be closed exactly once"
    multi_parts, wrong_parts, ok_parts = snapshots
    assert [name for _closed, name in multi_parts] == ["a.wav", "b.wav"]
    assert all(closed for closed, _name in multi_parts)
    assert [name for _closed, name in wrong_parts] == ["c.wav"]
    assert all(closed for closed, _name in wrong_parts)
    assert all(closed for closed, _name in ok_parts)


def test_cancellation_kills_running_converter(monkeypatch):
    started = threading.Event()
    seen = []

    def convert(path, container, max_bytes, timeout_s, cancel_event=None,
                binary="ffmpeg"):
        seen.append(cancel_event)
        started.set()
        if cancel_event is not None:
            cancel_event.wait(10)  # stand-in for the real ffmpeg read loop
        raise batchmod.AudioConversionCancelled()

    app, mock, _ = make_app(monkeypatch, converter=convert)

    async def scenario():
        async with async_client(app) as client:
            task = asyncio.create_task(client.post(PATH, headers=AUTH,
                                                   files=audio_files()))
            await _wait_event(started)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            return mock.requests

    requests = asyncio.run(scenario())
    assert seen and seen[0] is not None and seen[0].is_set()
    assert requests == [], "no ASR work may start for a cancelled conversion"


def test_total_deadline_returns_504_and_then_releases(monkeypatch):
    started, release = threading.Event(), threading.Event()
    mock = BlockingOllama(started, release)
    cfg = Config(token=WS_TOKEN, mobile_token=MOBILE_TOKEN,
                 ollama_url="http://mock-ollama", mobile_total_timeout_s=0.5)
    app, _, _ = make_app(monkeypatch, cfg=cfg, mock=mock,
                         converter=fake_converter(pcm=speech_pcm(0.5)))

    async def scenario():
        async with async_client(app) as client:
            task = asyncio.create_task(client.post(PATH, headers=AUTH,
                                                   files=audio_files()))
            await _wait_event(started)
            await asyncio.sleep(1.3)  # past the (clamped) 1 s deadline
            busy = await client.post(PATH, headers=AUTH, files=audio_files())
            still_running = not mock.finished.is_set()
            release.set()
            slow = await asyncio.wait_for(task, 10)
            after = await client.post(PATH, headers=AUTH, files=audio_files())
            return slow, busy, still_running, after

    slow, busy, still_running, after = asyncio.run(scenario())
    assert slow.status_code == 504
    assert slow.json()["error"] == "deadline_exceeded"
    assert busy.status_code == 503
    assert still_running
    assert after.status_code == 200


def test_converter_timeout_maps_to_504_and_cleans_temp(monkeypatch):
    def timeout_converter(path, container, max_bytes, timeout_s,
                          cancel_event=None, binary="ffmpeg"):
        if cancel_event is not None:
            cancel_event.wait(timeout_s)
        raise batchmod.AudioConversionTimeout()

    before = {p.name for p in Path(batchmod.tempfile.gettempdir()).glob(
        "fscribe-batch-*")}
    cfg = Config(token=WS_TOKEN, mobile_token=MOBILE_TOKEN,
                 ollama_url="http://mock-ollama", mobile_convert_timeout_s=0.2)
    app, mock, _ = make_app(monkeypatch, cfg=cfg, converter=timeout_converter)
    with TestClient(app) as client:
        resp = client.post(PATH, headers=AUTH, files=audio_files())
    assert resp.status_code == 504
    assert resp.json()["error"] == "deadline_exceeded"
    assert mock.requests == []
    after = {p.name for p in Path(batchmod.tempfile.gettempdir()).glob(
        "fscribe-batch-*")}
    assert after == before, "private convert temp files must be removed"


# ------------------------------------------- bounded subprocess behaviour ----

def test_run_bounded_command_reads_stdout_and_eof():
    out = batchmod.run_bounded_command(
        [sys.executable, "-c",
         "import sys; sys.stdout.buffer.write(b'\\x01\\x02' * 16000)"],
        max_bytes=64 * 1024, timeout_s=10)
    assert len(out) == 32000


def test_run_bounded_command_kills_on_timeout(tmp_path):
    marker = tmp_path / "finished"
    script = ("import pathlib, time\n"
              "time.sleep(3)\n"
              f"pathlib.Path({str(marker)!r}).write_text('done')\n")
    started = time.monotonic()
    with pytest.raises(batchmod.AudioConversionTimeout):
        batchmod.run_bounded_command([sys.executable, "-c", script],
                                     max_bytes=1 << 20, timeout_s=0.3)
    assert time.monotonic() - started < 2.5
    time.sleep(0.3)
    assert not marker.exists(), "timed-out subprocess must be killed, not left"


def test_run_bounded_command_caps_unbounded_output():
    script = ("import sys\n"
              "while True:\n"
              "    sys.stdout.buffer.write(b'\\x00' * 65536)\n")
    started = time.monotonic()
    with pytest.raises(batchmod.AudioTooLongError):
        batchmod.run_bounded_command([sys.executable, "-c", script],
                                     max_bytes=1 << 20, timeout_s=10)
    assert time.monotonic() - started < 5, "output cap must stop a duration bomb"


def test_run_bounded_command_honours_cancel_event():
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(batchmod.AudioConversionCancelled):
        batchmod.run_bounded_command(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            max_bytes=1 << 20, timeout_s=5, cancel_event=cancel)


def test_run_bounded_command_classifies_nonzero_exit():
    with pytest.raises(batchmod.AudioDecodeError):
        batchmod.run_bounded_command(
            [sys.executable, "-c", "raise SystemExit(3)"],
            max_bytes=1 << 20, timeout_s=10)


@pytest.mark.skipif(shutil.which("ffmpeg") is None,
                    reason="ffmpeg absent (Docker image smoke runs this)")
def test_real_ffmpeg_decodes_generated_wav(tmp_path):
    pcm = speech_pcm(1.0)
    source = tmp_path / "generated.wav"
    source.write_bytes(wav_bytes(pcm))
    out = batchmod.decode_with_ffmpeg(str(source), "wav", max_bytes=1 << 20,
                                      timeout_s=15)
    assert len(out) % 2 == 0
    assert abs(len(out) - len(pcm)) <= 16000 * 2 * 0.1  # <=100 ms boundary slack


@pytest.mark.skipif(shutil.which("ffmpeg") is None,
                    reason="ffmpeg absent (Docker image smoke runs this)")
def test_real_ffmpeg_m4a_upload_end_to_end(monkeypatch, tmp_path):
    """Encode synthetic speech to real M4A/AAC, then drive the full endpoint.

    Covers the deployment-critical path that stubs cannot: ffmpeg's `mov`
    demuxer, AAC decode to PCM16/16 kHz, the private spool, the 61 s gate and
    the Kotlin numeric response contract. Uses only generated audio.
    """
    ffmpeg = shutil.which("ffmpeg")
    pcm = speech_pcm(1.2)
    wav_path = tmp_path / "synthetic.wav"
    wav_path.write_bytes(wav_bytes(pcm))
    m4a_path = tmp_path / "synthetic.m4a"
    subprocess.run(
        [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "wav", "-i", str(wav_path),
         "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart",
         str(m4a_path)],
        check=True, timeout=30)
    upload = m4a_path.read_bytes()
    assert upload

    cfg = Config(token=WS_TOKEN, mobile_token=MOBILE_TOKEN,
                 ollama_url="http://mock-ollama", min_endpointing_ms=400,
                 partial_interval_s=0.2, mobile_ffmpeg_bin=ffmpeg)
    app, mock, _ = make_app(monkeypatch, cfg=cfg)
    with TestClient(app) as client:
        resp = client.post(PATH, headers=AUTH,
                           files={"file": ("recording.m4a", upload, "audio/mp4")})
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert parse_like_kotlin(payload) == "Unforced test text."
    assert abs(payload["audio_duration_ms"] - 1200) <= 150
    assert mock.requests


@pytest.mark.skipif(shutil.which("ffmpeg") is None,
                    reason="ffmpeg absent (Docker image smoke runs this)")
def test_real_ffmpeg_rejects_malformed_container(tmp_path):
    bad = tmp_path / "bad.m4a"
    bad.write_bytes(b"not-an-mp4-at-all" * 256)
    with pytest.raises(batchmod.AudioDecodeError):
        batchmod.decode_with_ffmpeg(str(bad), "mov", max_bytes=1 << 20,
                                    timeout_s=15)
