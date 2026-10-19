"""Real-server (uvicorn loopback) hermetic checks for deployment semantics:
- handshake rejection status for unauthenticated WS peers (401/403, not silent),
- prefixed health aliases answering identically to the root pair,
- one authenticated realtime session end-to-end over a real socket
  (mock Ollama + fake aligner; no external services, no timeout leaks)."""
import asyncio
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
import websockets

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fourscribe import app as appmod
from fourscribe.config import Config
from tests.conftest import FakeAligner, speech_pcm
from tests.mock_ollama import MockOllama


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _header_kwargs(hs):
    """websockets version compatibility (header kw arg renamed across majors)."""
    sig = pytest.importorskip("inspect").signature(websockets.connect)
    for name in ("additional_headers", "extra_headers"):
        if name in sig.parameters:
            return {name: hs}
    raise RuntimeError("unsupported websockets client version")


@pytest.fixture(scope="module")
def server_factory():
    servers = []

    def make(token="real-smoke-token"):
        mock = MockOllama(delay_s=0.05)
        cfg = Config(token=token, ollama_url="http://mock-ollama",
                     min_endpointing_ms=400, partial_interval_s=0.2)
        transport = mock.transport()
        app = appmod.create_app(
            cfg, lambda: httpx.Client(transport=transport, base_url=cfg.ollama_url),
            aligner=FakeAligner())
        port = _free_port()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                               log_level="error"))
        threading.Thread(target=server.run, daemon=True).start()
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started, "uvicorn did not start"
        servers.append(server)
        return f"127.0.0.1:{port}", mock

    yield make
    for s in servers:
        s.should_exit = True


def _ws_url(base, path="/v1/audio/transcriptions/realtime"):
    return f"ws://{base}{path}"


def test_unauthenticated_handshake_refused(server_factory):
    base, _ = server_factory()
    url = _ws_url(base)

    async def attempt():
        return await websockets.connect(url, open_timeout=10, **_header_kwargs({}))

    with pytest.raises(websockets.exceptions.InvalidStatus) as exc:
        asyncio.run(attempt())
    code = getattr(exc.value.response, "status_code", None)
    code_val = getattr(code, "value", code)
    assert int(code_val) in (401, 403), f"unexpected rejection status {code_val}"


def test_health_aliases_answer_identically(server_factory):
    base, _ = server_factory()

    def get(p):
        r = httpx.get(f"http://{base}{p}", timeout=5)
        return r.status_code, r.json()

    assert get("/healthz")[0] == 200
    assert get("/healthz") == get("/4scribe/healthz")
    assert get("/healthz")[1]["ok"] is True
    assert get("/4scribe/readyz")[1]["workers"] == 2


def test_authenticated_realtime_over_real_socket(server_factory):
    base, mock = server_factory()
    url = _ws_url(base, "/4scribe/v1/audio/transcriptions/realtime")
    events = []

    async def run():
        headers = {"authorization": "Bearer real-smoke-token"}
        ws = await websockets.connect(url, open_timeout=10, **_header_kwargs(headers))
        try:
            await ws.send(json.dumps({"type": "session.update", "session": {
                "sample_rate": 16000, "language": "ru", "endpointing_ms": 400,
                "word_timestamps": True}}))
            upd = json.loads(await ws.recv())
            assert upd["type"] == "session.updated"
            assert (upd.get("applied") or {}).get("language") == "ru"
            pcm = speech_pcm(1.5)
            for off in range(0, len(pcm), 3200):
                await ws.send(pcm[off:off + 3200])
            await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
            ack = None
            while ack is None and len(events) < 50:
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                events.append(ev)
                if ev.get("type") == "input_audio_buffer.committed":
                    ack = ev
            return ack
        finally:
            try:
                await ws.close()
            except Exception:
                pass

    ack = asyncio.run(run())
    assert ack is not None
    final = next(e for e in events
                 if e["type"] == "conversation.item.input_audio_transcription.completed")
    assert events.index(final) < events.index(ack), "final must precede commit ACK"
    text = final.get("transcript") or ""
    words = final.get("words") or []
    assert not text or [w["word"] for w in words] == text.split()
    assert not any(e["type"] == "error" for e in events)
