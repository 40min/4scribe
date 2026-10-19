"""Production-token startup guard (deployment PLAN): env-driven launch with
the default/missing token must fail closed; explicit Config (tests) unaffected.
Hermetic: no network, FakeAligner stand-in, mock base URL only."""
import pytest
from fastapi import WebSocket

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fourscribe import app as appmod
from fourscribe.config import Config
from fourscribe.verifier import NullVerifier
from tests.conftest import FakeAligner


class FakeHttp:
    def __init__(self):
        pass


def fake_http_factory():
    return FakeHttp()


def test_env_default_token_rejected(monkeypatch):
    monkeypatch.delenv("FSCRIBE_TOKEN", raising=False)
    monkeypatch.delenv("FSCRIBE_ALLOW_TEST_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="startup guard"):
        appmod.create_app()


def test_env_test_token_literal_rejected(monkeypatch):
    monkeypatch.setenv("FSCRIBE_TOKEN", "test-token")
    monkeypatch.delenv("FSCRIBE_ALLOW_TEST_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="startup guard"):
        appmod.create_app()


def test_env_real_token_accepted(monkeypatch):
    monkeypatch.setenv("FSCRIBE_TOKEN", "fscribe-live-0000000000000000")
    monkeypatch.setenv("FSCRIBE_OLLAMA_URL", "http://mock-ollama")
    monkeypatch.delenv("FSCRIBE_ALLOW_TEST_TOKEN", raising=False)
    app = appmod.create_app(aligner=FakeAligner(), verifier=NullVerifier())
    for path in appmod.REALTIME_WS_PATHS:
        assert any(route.path == path for route in app.router.routes)


def test_allow_test_token_optout(monkeypatch):
    monkeypatch.delenv("FSCRIBE_TOKEN", raising=False)
    monkeypatch.setenv("FSCRIBE_ALLOW_TEST_TOKEN", "1")
    app = appmod.create_app(aligner=FakeAligner(), verifier=NullVerifier())
    assert app


def test_explicit_config_bypasses_guard_but_aliases_registered(mock, monkeypatch):
    # Tests construct Config explicitly (as the whole suite does): the guard
    # must not fire, and both loopback + /4scribe-prefixed WS routes exist.
    monkeypatch.delenv("FSCRIBE_TOKEN", raising=False)
    cfg = Config(token="test-token", ollama_url="http://mock-ollama")
    app = appmod.create_app(cfg, lambda: FakeHttp(), aligner=FakeAligner())
    assert {r.path for r in app.router.routes} >= set(appmod.REALTIME_WS_PATHS)


def test_prefixed_route_serves_realtime_contract(client_factory):
    """The /4scribe-prefixed path answers identically (session.updated)."""
    import json
    client = client_factory(mock_delay=0.001)
    url = "ws://testserver/4scribe/v1/audio/transcriptions/realtime"
    with client.websocket_connect(url, headers={"authorization": "Bearer test-token"}) as ws:
        ws.send_text(json.dumps({"type": "session.update", "session": {
            "sample_rate": 16000, "language": "ru", "endpointing_ms": 800,
            "word_timestamps": True}}))
        upd = json.loads(ws.receive_text())
        assert upd["type"] == "session.updated"


def test_unprefixed_route_unchanged(client_factory):
    """Contract regression: the loopback /v1 path still serves the same flow."""
    import json
    client = client_factory(mock_delay=0.001)
    url = "ws://testserver/v1/audio/transcriptions/realtime"
    with client.websocket_connect(url, headers={"authorization": "Bearer test-token"}) as ws:
        ws.send_text(json.dumps({"type": "session.update", "session": {
            "sample_rate": 16000, "language": "en", "endpointing_ms": 800,
            "word_timestamps": True}}))
        upd = json.loads(ws.receive_text())
        assert upd["type"] == "session.updated"
