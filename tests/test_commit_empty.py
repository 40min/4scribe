"""Live-gate middle-phrase-omission regression: an EMPTY-buffer commit (the
normal case after every endpoint final drained the segment buffer) must NOT
produce a fatal wire error event and must ACK - decoding an empty buffer
previously emitted decode_error, which the REAL Loqui client treats as fatal
for the whole generation (restart WITHOUT replay), dropping middle phrases.
Hermetic: mock Ollama + fake aligner over a real uvicorn loopback port."""
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


@pytest.fixture
def service():
    mock = MockOllama(delay_s=0.2)
    cfg = Config(token="e-commit-token", ollama_url="http://mock-ollama",
                 min_endpointing_ms=400)
    app = appmod.create_app(
        cfg, lambda: httpx.Client(transport=mock.transport(),
                                  base_url=cfg.ollama_url),
        aligner=FakeAligner())
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started
    yield f"ws://127.0.0.1:{port}/4scribe/v1/audio/transcriptions/realtime"
    server.should_exit = True


def test_empty_buffer_commit_acks_without_fatal_error(service):
    events = []

    async def run():
        async with websockets.connect(service, open_timeout=10,
                                      additional_headers={"authorization": "Bearer e-commit-token"}) as ws:
            await ws.send(json.dumps({"type": "session.update", "session": {
                "sample_rate": 16000, "language": "ru", "endpointing_ms": 400,
                "word_timestamps": True}}))
            assert json.loads(await ws.recv())["type"] == "session.updated"

            async def reader():
                while True:
                    events.append(json.loads(await ws.recv()))

            r = asyncio.create_task(reader())
            try:
                # brief speech -> endpoint final
                await ws.send(speech_pcm(1.2))
                # TWO commits with EMPTY remaining buffers (silence since the
                # endpoint closed the phrase): previously -> decode_error
                await asyncio.sleep(0.6)
                await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                await asyncio.sleep(0.3)
                await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                await asyncio.sleep(1.5)
            finally:
                r.cancel()
                await asyncio.gather(r, return_exceptions=True)

    asyncio.run(run())
    kinds = [e.get("type") for e in events]
    assert "error" not in kinds, f"fatal error event leaked: {events}"
    acks = [e for e in events if e["type"] == "input_audio_buffer.committed"]
    assert len(acks) >= 2, "both commits must ACK (fence contract)"
    finals = [e for e in events if e["type"] == "conversation.item.input_audio_transcription.completed"]
    assert len(finals) == 1  # the endpoint final, never duplicated per commit
    # ACK strictly after the epoch's final (finals-before-ACK preserved)
    assert kinds.index("conversation.item.input_audio_transcription.completed") \
        < kinds.index("input_audio_buffer.committed")
