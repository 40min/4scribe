"""Standalone text enhancement API tests (behaviour-preserving corrector port).

Hermetic: an async httpx.MockTransport stands in for Ollama. Covers the
migrated prompt/options/response guards, the Loqui success contract plus raw
fallback fields, dedicated-token isolation, strict JSON/body bounds, streaming
body caps, one-slot admission, slow-upload deadline, cancellation/deadline
capacity retention, and app-lifecycle HTTP client cleanup.
"""
import asyncio
import json
import logging
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fourscribe import app as appmod
from fourscribe import enhancement as enhmod
from fourscribe.config import Config
from tests.conftest import FakeAligner

PATH = "/4scribe/v1/text/enhance"
#: What the tailnet ingress sends after stripping the public `/4scribe` prefix.
INGRESS_PATH = "/v1/text/enhance"
WS_TOKEN = "ws-realtime-0000000000"
MOBILE_TOKEN = "mobile-batch-0000000000"
ENH_TOKEN = "enhancement-text-000000"
AUTH = {"authorization": "Bearer " + ENH_TOKEN}
BASE = "http://testserver"


def chat_response(content, *, done=True, done_reason="stop", eval_count=5,
                  status=200):
    if status != 200:
        return httpx.Response(status, json={"error": "upstream"})
    return httpx.Response(status, json={
        "message": {"content": content},
        "done": done,
        "done_reason": done_reason,
        "eval_count": eval_count,
        "prompt_eval_count": 10,
    })


class EnhanceOllama:
    """Async mock Ollama that records request bodies and serves one responder."""

    def __init__(self, responder=None):
        self.requests = []
        self.responder = responder or self.echo

    async def echo(self, body):
        text = body["messages"][-1]["content"]
        return chat_response(text)

    def transport(self):
        async def handler(request):
            assert request.url.path == "/api/chat"
            body = json.loads(request.content)
            self.requests.append(body)
            return await self.responder(body)

        return httpx.MockTransport(handler)


def make_app(cfg=None, responder=None):
    cfg = cfg or Config(token=WS_TOKEN, mobile_token=MOBILE_TOKEN,
                        enhancement_token=ENH_TOKEN,
                        ollama_url="http://mock-ollama")
    mock = EnhanceOllama(responder)
    app = appmod.create_app(
        cfg,
        lambda: httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(404, json={})),
            base_url=cfg.ollama_url),
        aligner=FakeAligner(),
        enhancement_http_factory=lambda: httpx.AsyncClient(
            transport=mock.transport(), base_url=cfg.ollama_url))
    return app, mock, cfg


@asynccontextmanager
async def app_client(app):
    """Run FastAPI startup/shutdown around an ASGI async client."""
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url=BASE) as client:
            yield client


def make_real_upstream_app(cfg):
    """App whose enhancer uses a REAL httpx socket to cfg.ollama_url."""
    return appmod.create_app(
        cfg,
        lambda: httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(404, json={})),
            base_url=cfg.ollama_url),
        aligner=FakeAligner())


@asynccontextmanager
async def chat_server(*, first_delay=0.0, first_close=False, later_delay=0.0,
                      trickle_interval=None):
    """Tiny real local HTTP server with controllable generation timing.

    It reads the full request body before simulating generation. A client
    disconnect does not stop the simulated job; that is exactly the behaviour
    the admission policy must survive.
    """
    started = asyncio.Event()
    first_finished = asyncio.Event()
    state = {"started": 0}

    async def handler(reader, writer):
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            length = 0
            for line in header.split(b"\r\n")[1:]:
                name, _, value = line.partition(b":")
                if name.strip().lower() == b"content-length":
                    length = int(value.strip())
            body = await reader.readexactly(length)
            payload = json.loads(body)
            state["started"] += 1
            number = state["started"]
            started.set()
            if first_close and number == 1:
                writer.close()
                await writer.wait_closed()
                return
            await asyncio.sleep(first_delay if number == 1 else later_delay)
            if number == 1:
                first_finished.set()
            content = payload["messages"][-1]["content"]
            encoded = json.dumps({
                "message": {"content": content},
                "done": True,
                "done_reason": "stop",
                "eval_count": 5,
            }).encode()
            head = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    b"Content-Length: " + str(len(encoded)).encode() +
                    b"\r\nConnection: close\r\n\r\n")
            if trickle_interval is not None and number == 1:
                # Slow trickle: each gap is shorter than a typical httpx
                # inactivity timeout, but the whole body exceeds wall budget.
                writer.write(head)
                await writer.drain()
                for offset in range(0, len(encoded), 5):
                    writer.write(encoded[offset:offset + 5])
                    await writer.drain()
                    await asyncio.sleep(trickle_interval)
            else:
                writer.write(head + encoded)
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionError, RuntimeError):
                pass

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield server, port, started, first_finished, state
    finally:
        server.close()
        await server.wait_closed()


async def wait_unlocked(sem, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while sem.locked() and loop.time() < deadline:
        await asyncio.sleep(0.01)
    assert not sem.locked(), "admission slot was not released"


# ------------------------------------------------------------ basic contract ----

def test_corrected_success_has_loqui_and_raw_fallback_fields():
    app, mock, _ = make_app(responder=_value(chat_response("Привет, мир.")))
    with TestClient(app) as client:
        resp = client.post(PATH, headers=AUTH, json={"text": "привет мир"})
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "corrected",
        "text": "Привет, мир.",
        "raw_text": "привет мир",
        "enhanced_text": "Привет, мир.",
    }
    assert len(mock.requests) == 1


def _value(value):
    async def responder(_body):
        return value
    return responder


def test_unchanged_success_keeps_text_and_null_enhancement():
    app, _, _ = make_app(responder=_value(chat_response("Привет мир")))
    with TestClient(app) as client:
        resp = client.post(PATH, headers=AUTH, json={"text": "Привет мир"})
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "unchanged",
        "text": "Привет мир",
        "raw_text": "Привет мир",
        "enhanced_text": None,
    }


def test_raw_text_is_exact_when_migrated_corrector_strips_output():
    raw = "  Привет мир  "
    app, _, _ = make_app(responder=_value(chat_response(raw)))
    with TestClient(app) as client:
        resp = client.post(PATH, headers=AUTH, json={"text": raw})
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "corrected",
        "text": "Привет мир",
        "raw_text": raw,
        "enhanced_text": "Привет мир",
    }


def test_language_is_validated_but_not_injected_into_migrated_prompt():
    app, mock, _ = make_app()
    with TestClient(app) as client:
        assert client.post(PATH, headers=AUTH,
                           json={"text": "тест", "language": "ru"}).status_code == 200
    body = mock.requests[0]
    assert body["messages"] == [
        {"role": "system", "content": enhmod.ENHANCER_SYSTEM_PROMPT},
        {"role": "user", "content": "тест"},
    ]
    assert "language" not in body
    assert body["model"] == "qwen3:0.6b"
    assert body["stream"] is False
    assert body["think"] is False
    assert body["keep_alive"] == -1
    assert body["options"] == {"temperature": 0, "num_predict": 120,
                               "num_ctx": 4096}


def test_migrated_options_scale_with_input_length():
    assert enhmod._enhance_options(200)["num_predict"] == 120
    assert enhmod._enhance_options(201)["num_predict"] == 160
    assert enhmod._enhance_options(1300)["num_predict"] == 1024
    assert enhmod._enhance_options(4000)["num_predict"] == 1024
    assert enhmod._enhance_options(4000)["num_ctx"] == 4096


# --------------------------------------------------------------- auth path ----

def test_wrong_and_shared_tokens_are_rejected_before_body():
    app, mock, _ = make_app()
    with TestClient(app) as client:
        for headers in ({}, {"authorization": "Bearer nope"},
                        {"authorization": "Bearer " + WS_TOKEN},
                        {"authorization": "Bearer " + MOBILE_TOKEN},
                        {"authorization": ENH_TOKEN}):
            resp = client.post(PATH, headers=headers, json={"text": "тест"})
            assert resp.status_code == 401
            assert resp.json() == {"error": {"code": "unauthorized"}}
    assert mock.requests == []


def test_ingress_stripped_alias_has_identical_auth_and_contract():
    """Ingress strips /4scribe; the unprefixed route must behave identically."""
    app, _, _ = make_app(responder=_value(chat_response("Привет, мир.")))
    with TestClient(app) as client:
        for path in (PATH, INGRESS_PATH):
            assert client.post(path, headers={"authorization": "Bearer nope"},
                               json={"text": "тест"}).status_code == 401
            assert client.post(path, headers=AUTH,
                               json={"text": ""}).status_code == 422
        prefixed = client.post(PATH, headers=AUTH, json={"text": "привет мир"})
        stripped = client.post(INGRESS_PATH, headers=AUTH,
                               json={"text": "привет мир"})
    assert prefixed.status_code == stripped.status_code == 200
    assert prefixed.json() == stripped.json() == {
        "status": "corrected",
        "text": "Привет, мир.",
        "raw_text": "привет мир",
        "enhanced_text": "Привет, мир.",
    }


def test_ingress_stripped_alias_shares_one_admission_slot():
    started = None
    release = None

    async def blocking(_body):
        started.set()
        await release.wait()
        return chat_response("готово")

    app, mock, _ = make_app(responder=blocking)

    async def scenario():
        nonlocal started, release
        started, release = asyncio.Event(), asyncio.Event()
        async with app_client(app) as client:
            first = asyncio.create_task(
                client.post(PATH, headers=AUTH, json={"text": "первый"}))
            await asyncio.wait_for(started.wait(), 5)
            busy = await client.post(INGRESS_PATH, headers=AUTH,
                                     json={"text": "второй"})
            calls_while_busy = len(mock.requests)
            release.set()
            first_resp = await asyncio.wait_for(first, 5)
            after = await client.post(INGRESS_PATH, headers=AUTH,
                                      json={"text": "третий"})
            return busy, calls_while_busy, first_resp, after

    busy, calls, first_resp, after = asyncio.run(scenario())
    assert busy.status_code == 429
    assert calls == 1, "alias must use the same shared admission instance"
    assert first_resp.status_code == 200
    assert after.status_code == 200


@pytest.mark.parametrize("token,reason", [
    ("", "unset"),
    ("test-token", "placeholder"),
    ("short", "too_short"),
    (WS_TOKEN, "shared_with_realtime_token"),
    (MOBILE_TOKEN, "shared_with_mobile_token"),
])
def test_unsafe_configuration_fails_closed(token, reason):
    cfg = Config(token=WS_TOKEN, mobile_token=MOBILE_TOKEN,
                 enhancement_token=token, ollama_url="http://mock-ollama",
                 silero_enabled=False)
    app, mock, _ = make_app(cfg)
    with TestClient(app) as client:
        health = client.get("/healthz").json()["text_enhancement"]
        resp = client.post(PATH, headers={"authorization": "Bearer " + token},
                           json={"text": "тест"})
    assert health == {"enabled": False, "reason": reason,
                      "completion_unknown": False}
    assert resp.status_code == 503
    assert resp.json() == {"error": {"code": "enhancement_unavailable"}}
    assert mock.requests == []


def test_enabled_health_status():
    app, _, _ = make_app()
    with TestClient(app) as client:
        assert client.get("/healthz").json()["text_enhancement"] == {
            "enabled": True, "reason": "ok", "completion_unknown": False}


# -------------------------------------------------------- strict JSON input ----

@pytest.mark.parametrize("body", [
    b"not json",
    b"[]",
    b"\"text\"",
    b"null",
    b"123",
    b"{}",
    b'{"text": ""}',
    b'{"text": "   "}',
    b'{"text": 12}',
    b'{"text": null}',
    b'{"text": "ok", "language": "de"}',
    b'{"text": "ok", "language": null}',
])
def test_invalid_json_payloads_are_422_without_model_call(body):
    app, mock, _ = make_app()
    headers = {"authorization": "Bearer " + ENH_TOKEN,
               "content-type": "application/json"}
    with TestClient(app) as client:
        resp = client.post(PATH, headers=headers, content=body)
    assert resp.status_code == 422
    assert resp.json() == {"error": {"code": "invalid_input"}}
    assert mock.requests == []


def test_wrong_content_type_is_invalid_input():
    app, mock, _ = make_app()
    with TestClient(app) as client:
        resp = client.post(PATH, headers={**AUTH,
                                          "content-type": "text/plain"},
                           content=b'{"text": "ok"}')
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_input"
    assert mock.requests == []


def test_text_over_limit_is_rejected_not_truncated():
    app, mock, _ = make_app()
    with TestClient(app) as client:
        resp = client.post(PATH, headers=AUTH,
                           json={"text": "a" * (enhmod.MAX_TEXT_CHARS + 1)})
        at_limit = client.post(PATH, headers=AUTH,
                               json={"text": "a" * enhmod.MAX_TEXT_CHARS})
    assert resp.status_code == 413
    assert resp.json() == {"error": {"code": "input_too_large"}}
    assert at_limit.status_code == 200, "exactly 4000 chars must be accepted"
    assert len(mock.requests) == 1
    assert len(mock.requests[0]["messages"][-1]["content"]) == 4000


# ------------------------------------------------------- bounded body read ----

class StubStream:
    def __init__(self, chunks, headers=None):
        self.headers = headers or {}
        self._chunks = chunks

    async def stream(self):
        for chunk in self._chunks:
            yield chunk


def test_body_cap_counts_chunks_without_content_length():
    stub = StubStream([b"a" * 1000, b"b" * (enhmod.BODY_LIMIT + 1)])
    with pytest.raises(enhmod.BodyTooLarge):
        asyncio.run(enhmod.read_bounded_body(stub))


def test_body_cap_rejects_declared_oversize_early():
    stub = StubStream([], headers={"content-length": str(enhmod.BODY_LIMIT + 1)})
    with pytest.raises(enhmod.BodyTooLarge):
        asyncio.run(enhmod.read_bounded_body(stub))


def test_body_cap_ignores_understated_content_length():
    stub = StubStream([b"a" * 2048], headers={"content-length": "10"})
    assert asyncio.run(enhmod.read_bounded_body(stub)) == b"a" * 2048


def test_oversized_chunked_json_body_is_413_before_model_call():
    app, mock, _ = make_app()
    payload = b"x" * (enhmod.BODY_LIMIT + 1)
    with TestClient(app) as client:
        resp = client.post(PATH,
                           headers={"authorization": "Bearer " + ENH_TOKEN,
                                    "content-type": "application/json"},
                           content=iter([payload]))
    assert resp.status_code == 413
    assert resp.json() == {"error": {"code": "input_too_large"}}
    assert mock.requests == []


# ----------------------------------------------------- upstream response guards ----

@pytest.mark.parametrize("response,text", [
    (httpx.Response(200, content=b"not json"), "ok"),
    (httpx.Response(200, json=[]), "ok"),
    (httpx.Response(200, json={"done": False,
                               "message": {"content": "ok"}}), "ok"),
    (httpx.Response(200, json={"done": True}), "ok"),
    (httpx.Response(200, json={"done": True, "message": {"content": 12}}), "ok"),
    (chat_response(""), "ok"),
    (chat_response("   "), "ok"),
    (chat_response("a" * 201), "ok"),
    (chat_response("ok", done_reason="length"), "ok"),
    (chat_response("ok", eval_count=120), "ok"),
    (chat_response("too short"), "a" * 300),
])
def test_malformed_truncated_empty_and_collapsed_output_is_502(response, text):
    app, _, _ = make_app(responder=_value(response))
    with TestClient(app) as client:
        resp = client.post(PATH, headers=AUTH, json={"text": text})
    assert resp.status_code == 502
    assert resp.json() == {"error": {"code": "invalid_model_output"}}


def test_upstream_http_failure_is_503():
    app, _, _ = make_app(responder=_value(chat_response("x", status=500)))
    with TestClient(app) as client:
        resp = client.post(PATH, headers=AUTH, json={"text": "тест"})
    assert resp.status_code == 503
    assert resp.json() == {"error": {"code": "enhancement_unavailable"}}


def test_oversized_upstream_body_is_bounded_and_rejected():
    too_big = httpx.Response(200, content=b"x" * (enhmod.MAX_RESPONSE_BYTES + 1))
    app, _, _ = make_app(responder=_value(too_big))
    with TestClient(app) as client:
        resp = client.post(PATH, headers=AUTH, json={"text": "тест"})
    assert resp.status_code == 503
    assert resp.json() == {"error": {"code": "enhancement_unavailable"}}


def test_client_deadline_is_504_with_still_running_upstream():
    cfg = Config(token=WS_TOKEN, mobile_token=MOBILE_TOKEN,
                 enhancement_token=ENH_TOKEN, ollama_url="http://mock-ollama",
                 enhancement_deadline_s=0.2,
                 enhancement_upstream_timeout_s=2.0, silero_enabled=False)

    async def slow(_body):
        await asyncio.sleep(1.0)
        return chat_response("поздно")

    app, _, _ = make_app(cfg, responder=slow)

    async def scenario():
        async with app_client(app) as client:
            return await client.post(PATH, headers=AUTH, json={"text": "тест"})

    resp = asyncio.run(scenario())
    assert resp.status_code == 504
    assert resp.json() == {"error": {"code": "enhancement_timeout"}}


# ------------------------------------------------------- admission/lifecycle ----

def test_second_request_is_429_busy_and_not_queued():
    started = None
    release = None

    async def blocking(_body):
        started.set()
        await release.wait()
        return chat_response("готово")

    app, mock, _ = make_app(responder=blocking)

    async def scenario():
        nonlocal started, release
        started, release = asyncio.Event(), asyncio.Event()
        async with app_client(app) as client:
            first = asyncio.create_task(
                client.post(PATH, headers=AUTH, json={"text": "первый"}))
            await asyncio.wait_for(started.wait(), 5)
            second = await client.post(PATH, headers=AUTH,
                                       json={"text": "второй"})
            calls_while_busy = len(mock.requests)
            release.set()
            first_resp = await asyncio.wait_for(first, 5)
            after = await client.post(PATH, headers=AUTH, json={"text": "третий"})
            return second, calls_while_busy, first_resp, after

    second, calls, first_resp, after = asyncio.run(scenario())
    assert second.status_code == 429
    assert second.json() == {"error": {"code": "enhancement_busy"}}
    assert calls == 1, "busy request must not reach Ollama"
    assert first_resp.status_code == 200
    assert after.status_code == 200


def test_cancellation_keeps_slot_until_submitted_work_completes():
    started = None
    release = None
    finished = None

    async def blocking(_body):
        started.set()
        try:
            await release.wait()
        finally:
            finished.set()
        return chat_response("готово")

    app, _, _ = make_app(responder=blocking)
    sem = app.state.text_enhancer._slot

    async def scenario():
        nonlocal started, release, finished
        started, release = asyncio.Event(), asyncio.Event()
        finished = asyncio.Event()
        async with app_client(app) as client:
            task = asyncio.create_task(
                client.post(PATH, headers=AUTH, json={"text": "первый"}))
            await asyncio.wait_for(started.wait(), 5)
            task.cancel()
            await asyncio.sleep(0.05)
            busy = await client.post(PATH, headers=AUTH, json={"text": "второй"})
            held = sem.locked()
            still_running = not finished.is_set()
            release.set()
            await asyncio.wait_for(finished.wait(), 5)
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            await wait_unlocked(sem)
            after = await client.post(PATH, headers=AUTH, json={"text": "третий"})
            return busy, held, still_running, after

    busy, held, still_running, after = asyncio.run(scenario())
    assert busy.status_code == 429
    assert held, "cancel must not release admission before native work ends"
    assert still_running, "submitted work must not be treated as cancelled"
    assert after.status_code == 200


def test_deadline_timeout_keeps_slot_until_submitted_work_completes(monkeypatch):
    started = None
    release = None
    finished = None
    cfg = Config(token=WS_TOKEN, mobile_token=MOBILE_TOKEN,
                 enhancement_token=ENH_TOKEN, ollama_url="http://mock-ollama",
                 enhancement_deadline_s=0.2, silero_enabled=False)
    app, _, _ = make_app(cfg)
    sem = app.state.text_enhancer._slot

    async def slow_correct(_text, _timeout_s):
        started.set()
        try:
            await release.wait()
            return "готово"
        finally:
            finished.set()

    monkeypatch.setattr(app.state.text_enhancer, "_ollama_correct", slow_correct)

    async def scenario():
        nonlocal started, release, finished
        started, release = asyncio.Event(), asyncio.Event()
        finished = asyncio.Event()
        async with app_client(app) as client:
            task = asyncio.create_task(
                client.post(PATH, headers=AUTH, json={"text": "первый"}))
            await asyncio.wait_for(started.wait(), 5)
            slow = await asyncio.wait_for(task, 5)
            busy = await client.post(PATH, headers=AUTH, json={"text": "второй"})
            held = sem.locked()
            still_running = not finished.is_set()
            release.set()
            await asyncio.wait_for(finished.wait(), 5)
            await wait_unlocked(sem)
            after = await client.post(PATH, headers=AUTH, json={"text": "третий"})
            return slow, busy, held, still_running, after

    slow, busy, held, still_running, after = asyncio.run(scenario())
    assert slow.status_code == 504
    assert slow.json() == {"error": {"code": "enhancement_timeout"}}
    assert busy.status_code == 429
    assert held, "the 504 must not release admission while work still runs"
    assert still_running
    assert after.status_code == 200


def test_real_socket_client_504_keeps_admission_until_upstream_completes():
    """Real httpx socket timeouts, not MockTransport's no-timeout handler.

    The local server keeps its simulated generation running after the client
    response deadline. The first request gets 504, a concurrent request stays
    busy, and admission returns only after the observed upstream completion.
    """
    async def scenario():
        async with chat_server(first_delay=0.8, later_delay=0.0) as (
                _server, port, started, finished, state):
            cfg = Config(
                token=WS_TOKEN, mobile_token=MOBILE_TOKEN,
                enhancement_token=ENH_TOKEN,
                ollama_url=f"http://127.0.0.1:{port}",
                enhancement_deadline_s=0.1,
                enhancement_upstream_timeout_s=3.0, silero_enabled=False)
            app = make_real_upstream_app(cfg)
            sem = app.state.text_enhancer._slot
            async with app_client(app) as client:
                task = asyncio.create_task(
                    client.post(PATH, headers=AUTH, json={"text": "тест"}))
                await asyncio.wait_for(started.wait(), 2)
                first = await asyncio.wait_for(task, 2)
                await asyncio.sleep(0.03)
                busy = await client.post(PATH, headers=AUTH,
                                         json={"text": "второй"})
                still_running = not finished.is_set()
                held = sem.locked()
                await asyncio.wait_for(finished.wait(), 2)
                await wait_unlocked(sem)
                after = await client.post(PATH, headers=AUTH,
                                          json={"text": "третий"})
                return first, busy, still_running, held, after, state

    first, busy, still_running, held, after, state = asyncio.run(scenario())
    assert first.status_code == 504
    assert busy.status_code == 429
    assert held, "a real client timeout must not release admission early"
    assert still_running, "the local upstream job must still be generating"
    assert after.status_code == 200, "admission recovers on observed completion"
    assert state["started"] == 2, "the busy request must never reach upstream"


def test_real_transport_loss_latches_closed_and_restart_recovers():
    """Unknown native completion fails closed; a new app instance recovers."""
    async def scenario():
        async with chat_server(first_close=True, later_delay=0.0) as (
                _server, port, _started, _finished, state):
            cfg = Config(
                token=WS_TOKEN, mobile_token=MOBILE_TOKEN,
                enhancement_token=ENH_TOKEN,
                ollama_url=f"http://127.0.0.1:{port}",
                enhancement_deadline_s=2.0,
                enhancement_upstream_timeout_s=5.0, silero_enabled=False)
            app = make_real_upstream_app(cfg)
            async with app_client(app) as client:
                first = await client.post(PATH, headers=AUTH,
                                          json={"text": "первый"})
                second = await client.post(PATH, headers=AUTH,
                                           json={"text": "второй"})
                health_resp = await client.get("/healthz")
                health = health_resp.json()["text_enhancement"]
                calls_with_latch = state["started"]
            # A fresh app instance is the documented safe recovery (restart).
            app2 = make_real_upstream_app(cfg)
            async with app_client(app2) as client2:
                after = await client2.post(PATH, headers=AUTH,
                                           json={"text": "третий"})
            return first, second, health, calls_with_latch, after, state

    first, second, health, calls, after, state = asyncio.run(scenario())
    assert first.status_code == 503
    assert first.json() == {"error": {"code": "enhancement_unavailable"}}
    assert second.status_code == 503
    assert second.json() == {"error": {"code": "enhancement_unavailable"}}
    assert health["completion_unknown"] is True
    assert calls == 1, "latched endpoint must not start another generation"
    assert after.status_code == 200, "restart clears the explicit latch"
    assert state["started"] == 2


def test_real_exhausted_upstream_budget_latches_unknown_completion():
    """An upstream read timeout is not proof that generation stopped."""
    async def scenario():
        async with chat_server(first_delay=0.8, later_delay=0.0) as (
                _server, port, started, _finished, state):
            cfg = Config(
                token=WS_TOKEN, mobile_token=MOBILE_TOKEN,
                enhancement_token=ENH_TOKEN,
                ollama_url=f"http://127.0.0.1:{port}",
                enhancement_deadline_s=0.1,
                enhancement_upstream_timeout_s=0.25, silero_enabled=False)
            app = make_real_upstream_app(cfg)
            enhancer = app.state.text_enhancer
            async with app_client(app) as client:
                task = asyncio.create_task(
                    client.post(PATH, headers=AUTH, json={"text": "первый"}))
                await asyncio.wait_for(started.wait(), 2)
                first = await asyncio.wait_for(task, 2)
                for _ in range(100):
                    if enhancer._completion_unknown:
                        break
                    await asyncio.sleep(0.01)
                second = await client.post(PATH, headers=AUTH,
                                           json={"text": "второй"})
                return first, second, enhancer._completion_unknown, state

    first, second, latched, state = asyncio.run(scenario())
    assert first.status_code == 504
    assert latched, "exhausted upstream budget must latch unknown completion"
    assert second.status_code == 503
    assert second.json() == {"error": {"code": "enhancement_unavailable"}}
    assert state["started"] == 1, "latched endpoint must not start new work"


def test_slow_trickle_is_cut_by_wall_clock_budget_and_latches():
    """Tiny chunks inside httpx inactivity limits must still hit wall clock."""
    async def scenario():
        async with chat_server(trickle_interval=0.1, later_delay=0.0) as (
                _server, port, _started, _finished, state):
            cfg = Config(
                token=WS_TOKEN, mobile_token=MOBILE_TOKEN,
                enhancement_token=ENH_TOKEN,
                ollama_url=f"http://127.0.0.1:{port}",
                enhancement_deadline_s=3.0,
                enhancement_upstream_timeout_s=0.2, silero_enabled=False)
            app = make_real_upstream_app(cfg)
            enhancer = app.state.text_enhancer
            async with app_client(app) as client:
                began = time.monotonic()
                first = await client.post(PATH, headers=AUTH,
                                          json={"text": "первый"})
                elapsed = time.monotonic() - began
                latched = enhancer._completion_unknown
                second = await client.post(PATH, headers=AUTH,
                                           json={"text": "второй"})
                return first, elapsed, latched, second, state

    first, elapsed, latched, second, state = asyncio.run(scenario())
    assert first.status_code == 503
    assert first.json() == {"error": {"code": "enhancement_unavailable"}}
    assert elapsed < 1.0, "wall-clock budget must beat the multi-second trickle"
    assert latched, "exhausted wall-clock budget means unknown completion"
    assert second.status_code == 503
    assert state["started"] == 1, "latched endpoint must not admit second work"


def test_slow_trickle_within_wall_budget_still_succeeds():
    async def scenario():
        async with chat_server(trickle_interval=0.02, later_delay=0.0) as (
                _server, port, _started, _finished, _state):
            cfg = Config(
                token=WS_TOKEN, mobile_token=MOBILE_TOKEN,
                enhancement_token=ENH_TOKEN,
                ollama_url=f"http://127.0.0.1:{port}",
                enhancement_deadline_s=3.0,
                enhancement_upstream_timeout_s=2.0, silero_enabled=False)
            app = make_real_upstream_app(cfg)
            async with app_client(app) as client:
                resp = await client.post(PATH, headers=AUTH,
                                         json={"text": "тест"})
                return resp, app.state.text_enhancer._completion_unknown

    resp, latched = asyncio.run(scenario())
    assert resp.status_code == 200
    assert resp.json()["status"] == "unchanged"
    assert resp.json()["text"] == "тест"
    assert latched is False


def test_shutdown_is_bounded_closes_client_and_consumes_tasks():
    started = None
    release = None

    async def blocking(_body):
        started.set()
        await release.wait()  # must be cancelled by shutdown, not awaited out
        return chat_response("поздно")

    app, _, _ = make_app(responder=blocking)

    async def scenario():
        nonlocal started, release
        started, release = asyncio.Event(), asyncio.Event()
        async with app_client(app) as client:
            enhancer = app.state.text_enhancer
            task = asyncio.create_task(
                client.post(PATH, headers=AUTH, json={"text": "первый"}))
            await asyncio.wait_for(started.wait(), 2)
            owned = enhancer._client
            began = time.monotonic()
            await asyncio.wait_for(enhancer.close(), 3)
            elapsed = time.monotonic() - began
            result = await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)
            return (elapsed, owned, enhancer, result)

    elapsed, owned, enhancer, result = asyncio.run(scenario())
    assert elapsed < 2.5, "shutdown must be bounded"
    assert owned.is_closed
    assert enhancer._client is None
    assert not enhancer._work_tasks
    assert not enhancer._retained
    assert not enhancer._slot.locked(), "shutdown must not leak admission"
    assert result, "the cancelled endpoint task must be consumed"


def test_slow_upload_is_bounded_by_total_deadline_and_releases_slot():
    cfg = Config(token=WS_TOKEN, mobile_token=MOBILE_TOKEN,
                 enhancement_token=ENH_TOKEN, ollama_url="http://mock-ollama",
                 enhancement_deadline_s=0.3, silero_enabled=False)
    app, mock, _ = make_app(cfg)

    async def drip():
        for _ in range(20):
            await asyncio.sleep(0.1)
            yield b"x" * 32

    async def scenario():
        async with app_client(app) as client:
            headers = {"authorization": "Bearer " + ENH_TOKEN,
                       "content-type": "application/json"}
            resp = await client.post(PATH, headers=headers, content=drip())
            after = await client.post(PATH, headers=AUTH, json={"text": "тест"})
            return resp, after

    resp, after = asyncio.run(scenario())
    assert resp.status_code == 504
    assert resp.json() == {"error": {"code": "enhancement_timeout"}}
    assert after.status_code == 200, "slow-upload deadline must release admission"
    assert mock.requests, "the follow-up request must reach the mock model"


def test_text_admission_is_separate_from_asr_decode_semaphore():
    cfg = Config(token=WS_TOKEN, mobile_token=MOBILE_TOKEN,
                 enhancement_token=ENH_TOKEN, ollama_url="http://mock-ollama",
                 inference_workers=1, silero_enabled=False)
    app, _, _ = make_app(cfg)
    assert app.state.text_enhancer._slot is not app.state.decode_sem

    async def scenario():
        async with app_client(app) as client:
            await app.state.decode_sem.acquire()  # occupy ALL ASR capacity
            try:
                return await client.post(PATH, headers=AUTH,
                                         json={"text": "тест"})
            finally:
                app.state.decode_sem.release()

    resp = asyncio.run(scenario())
    assert resp.status_code == 200, "text correction must not need ASR capacity"


def test_async_http_client_is_created_and_closed_by_app_lifecycle():
    app, _, _ = make_app()
    with TestClient(app) as client:
        owned = app.state.text_enhancer._client
        assert owned is not None and not owned.is_closed
        assert client.post(PATH, headers=AUTH,
                           json={"text": "тест"}).status_code == 200
    assert owned.is_closed, "app shutdown must close the async client"
    assert app.state.text_enhancer._client is None


def test_logs_carry_no_text_or_token(caplog):
    raw, corrected = "секретный исходный текст", "Секретный исходный текст."
    app, _, _ = make_app(responder=_value(chat_response(corrected)))
    with caplog.at_level(logging.INFO, logger="fscribe.enhancement"):
        with TestClient(app) as client:
            assert client.post(PATH, headers=AUTH,
                               json={"text": raw}).status_code == 200
    assert raw not in caplog.text
    assert corrected not in caplog.text
    assert ENH_TOKEN not in caplog.text


def test_realtime_and_batch_routes_are_unchanged_by_addition():
    app, _, _ = make_app()
    paths = {getattr(route, "path", None) for route in app.routes}
    assert PATH in paths
    assert INGRESS_PATH in paths
    endpoints = {getattr(route, "path", None): getattr(route, "endpoint", None)
                 for route in app.routes}
    prefixed, stripped = endpoints[PATH], endpoints[INGRESS_PATH]
    assert prefixed.__self__ is stripped.__self__
    assert prefixed.__func__ is stripped.__func__, \
        "both routes must use the same authenticated handler/admission"
    assert "/4scribe/v1/audio/transcriptions" in paths
    assert "/v1/audio/transcriptions/realtime" in paths
    assert "/4scribe/v1/audio/transcriptions/realtime" in paths
