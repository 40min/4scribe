"""ASR response-contract tests: valid empty vs malformed/truncated/transport.

These call the REAL OllamaChat.decode over httpx.MockTransport and a real
websocket service loop with an empty-content mock response. No model calls.
"""
import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fourscribe import app as appmod
from fourscribe.asr import OllamaChat, OllamaDecodeError
from fourscribe.config import Config
from tests.conftest import FakeAligner, speech_pcm


def _chat(handler):
    cfg = Config(token="test-token", ollama_url="http://mock")
    client = httpx.Client(base_url="http://mock", transport=httpx.MockTransport(handler))
    return OllamaChat(cfg, http=client), client


def _decode(handler):
    chat, client = _chat(handler)
    try:
        return chat.decode(b"\x00\x00" * 1600, language=None)
    finally:
        client.close()


def test_valid_text_response():
    r = _decode(lambda req: httpx.Response(200, json={
        "message": {"content": "language English<asr_text>hello"},
        "done": True, "prompt_eval_count": 10, "eval_count": 2}))
    assert r.text == "hello"


@pytest.mark.parametrize("content", ["", "   ", "\n\t ", "language None<asr_text>"])
def test_valid_empty_or_no_speech_is_not_an_error(content):
    r = _decode(lambda req: httpx.Response(200, json={
        "message": {"content": content}, "done": True}))
    assert r.text == ""


@pytest.mark.parametrize("payload", [
    {"done": True},
    {"message": {}, "done": True},
    {"message": {"content": None}, "done": True},
    {"message": {"content": ["x"]}, "done": True},
    {"message": {"content": "text"}},
    {"message": {"content": "text"}, "done": False},
    {"message": {"content": "text"}, "done": "true"},
    ["not", "an", "object"],
])
def test_missing_malformed_or_truncated_fields_are_errors(payload):
    with pytest.raises(OllamaDecodeError):
        _decode(lambda req: httpx.Response(200, json=payload))


def test_http_and_transport_errors_stay_errors():
    with pytest.raises(httpx.HTTPStatusError):
        _decode(lambda req: httpx.Response(500, content=b"boom"))

    def boom(req):
        raise httpx.ConnectError("no route", request=req)

    with pytest.raises(httpx.ConnectError):
        _decode(boom)


class EmptyOllama:
    """Mock chat that returns a valid completed response with empty content."""

    def __init__(self):
        self.requests = 0

    def transport(self):
        def handler(request):
            if request.url.path == "/api/chat":
                self.requests += 1
                return httpx.Response(200, json={"message": {"content": ""}, "done": True})
            if request.url.path == "/api/tags":
                return httpx.Response(200, json={"models": []})
            if request.url.path.endswith("/api/generate"):
                return httpx.Response(200, json={})
            return httpx.Response(404, json={})
        return httpx.MockTransport(handler)


def test_valid_empty_asr_commit_acks_without_error_or_disconnect():
    empty = EmptyOllama()
    cfg = Config(token="test-token", ollama_url="http://mock", min_endpointing_ms=800,
                 silero_enabled=False)
    app = appmod.create_app(
        cfg, lambda: httpx.Client(transport=empty.transport(), base_url=cfg.ollama_url),
        aligner=FakeAligner())
    url = "ws://testserver/v1/audio/transcriptions/realtime"
    with TestClient(app) as client:
        with client.websocket_connect(url, headers={"authorization": "Bearer test-token"}) as ws:
            ws.send_text(json.dumps({"type": "session.update", "session": {
                "sample_rate": 16000, "language": "ru", "endpointing_ms": 800}}))
            assert json.loads(ws.receive_text())["type"] == "session.updated"
            ws.send_bytes(speech_pcm(0.6))
            ws.send_text(json.dumps({"type": "input_audio_buffer.commit"}))
            got = []
            for _ in range(40):
                ev = json.loads(ws.receive_text())
                got.append(ev)
                if ev["type"] == "input_audio_buffer.committed":
                    break
            kinds = [e["type"] for e in got]
            assert "error" not in kinds, f"valid empty ASR must not be fatal: {got}"
            assert "conversation.item.input_audio_transcription.completed" not in kinds
            assert "input_audio_buffer.committed" in kinds
            assert empty.requests >= 1
            # socket must still be usable (no client-fatal disconnect)
            ws.send_text(json.dumps({"type": "session.update", "session": {
                "sample_rate": 16000, "language": "en"}}))
            assert json.loads(ws.receive_text())["type"] == "session.updated"
