"""Wire-level realtime tests (pytest): auth, settings, silence gate, finals,
epoch ACK, two-source isolation, deltas, cancel/generation, bounded buffers."""
import json

import httpx
import pytest

from tests.mock_ollama import MockOllama, TEXT, CONT
from tests.conftest import URL, open_session, drain_until, speech_pcm, silence_pcm
from fourscribe.config import Config
from fourscribe import app as appmod
from fastapi.testclient import TestClient


# 1 auth
def test_auth_requires_bearer(client):
    with pytest.raises(Exception):
        with client.websocket_connect(URL):
            pass
    with pytest.raises(Exception):
        with client.websocket_connect(URL, headers={"authorization": "Bearer wrong"}):
            pass


# 2 session.update validation
def test_unsupported_language_rejected_not_applied(client):
    for ws, _ in open_session(client, lang=None):
        ws.send_text(json.dumps({"type": "session.update",
                                 "session": {"language": "auto", "sample_rate": 16000}}))
        upd = json.loads(ws.receive_text())
        assert upd["type"] == "session.updated"
        assert upd["error"]["code"] == "unsupported_language"
        assert "language" not in upd["applied"]


def test_sample_rate_rejected(client):
    for ws, _ in open_session(client, lang=None):
        ws.send_text(json.dumps({"type": "session.update",
                                 "session": {"language": "en", "sample_rate": 48000}}))
        upd = json.loads(ws.receive_text())
        assert upd["error"]["code"] == "invalid_sample_rate"


def test_endpointing_clamped_to_floor(client):
    for ws, upd in open_session(client, lang="en", endpointing=100):
        assert upd["applied"]["endpointing_ms"] >= 800


def test_word_timestamps_false_when_aligner_unavailable(client_factory):
    tc = client_factory(aligner="unavailable")
    for ws, upd in open_session(tc, lang="en", word_timestamps=True):
        assert upd["applied"]["words"] is False
        assert upd["error"]["code"] == "word_timestamps_unavailable"


def test_word_timestamps_applied_when_aligner_available(client):
    for ws, upd in open_session(client, lang="en", word_timestamps=True):
        assert upd["applied"]["words"] is True
        assert "error" not in upd


# 3 silence must not decode
def test_silence_does_not_call_decoder(client_factory):
    m = MockOllama(delay_s=0.02)
    cfg = Config(token="test-token", ollama_url="http://mock-ollama")
    tc = client_factory(cfg)
    for ws, _ in open_session(tc, lang="en"):
        ws.send_bytes(silence_pcm(1.2))
        ws.send_text(json.dumps({"type": "input_audio_buffer.commit"}))
        drain_until(ws, ("input_audio_buffer.committed",))
        assert m.requests == []


# 4 final path: endpoint final, real words, epoch mapping, ACK
def test_speech_final_and_commit_ack(client):
    for ws, _ in open_session(client, lang="ru"):
        ws.send_bytes(speech_pcm(1.5))
        ws.send_bytes(silence_pcm(1.2))
        ws.send_text(json.dumps({"type": "input_audio_buffer.commit"}))
        evs = drain_until(ws, ("conversation.item.input_audio_transcription.completed",
                               "input_audio_buffer.committed"))
        kinds = [e["type"] for e in evs]
        assert "conversation.item.input_audio_transcription.completed" in kinds
        assert "input_audio_buffer.committed" in kinds
        fin = [e for e in evs if e["type"].endswith(".completed")][0]
        ack = [e for e in evs if e["type"] == "input_audio_buffer.committed"][0]
        assert fin["transcript"] == CONT["ru"].strip()
        # words: exact final text, finite ordered non-overlapping, inside epoch audio
        words = fin["words"]
        assert [w["word"] for w in words] == CONT["ru"].strip().split()
        assert fin["alignment"]["status"] == "ok"
        prev_end = 0.0
        for w in words:
            assert 0 <= w["start"] <= w["end"]
            assert w["start"] >= prev_end - 1e-6
            prev_end = w["end"]
        assert words[-1]["end"] <= fin["audio_processed"] + 0.05
        assert fin["audio_processed"] > 0.5
        assert ack["audio_processed"] == 0.0
        ids = [e["event_id"] for e in evs]
        assert len(ids) == len(set(ids))


# 5 two sources isolated
def test_two_sources_isolated(client):
    for ws1, _ in open_session(client, lang="ru"):
        ws1.send_bytes(speech_pcm(1.5))
        ws1.send_bytes(silence_pcm(1.2))
        evs1 = drain_until(ws1, ("conversation.item.input_audio_transcription.completed",))
    for ws2, _ in open_session(client, lang="en"):
        ws2.send_bytes(speech_pcm(1.5))
        ws2.send_bytes(silence_pcm(1.2))
        evs2 = drain_until(ws2, ("conversation.item.input_audio_transcription.completed",))
    fin1 = [e for e in evs1 if e["type"].endswith(".completed")][0]
    fin2 = [e for e in evs2 if e["type"].endswith(".completed")][0]
    assert fin1["transcript"] == CONT["ru"].strip()
    assert fin2["transcript"] == CONT["en"].strip()
    assert fin1["event_id"] != fin2["event_id"]
    assert fin1["words"] and fin2["words"]


# 6 epoch resets + strict finals-before-ACK ordering across two commits
def test_epoch_reset_and_finals_before_ack(client):
    for ws, _ in open_session(client, lang="ru"):
        for _ in range(2):
            ws.send_bytes(speech_pcm(1.5))
            ws.send_bytes(silence_pcm(1.2))
            ws.send_text(json.dumps({"type": "input_audio_buffer.commit"}))
            evs = drain_until(ws, ("conversation.item.input_audio_transcription.completed",
                                   "input_audio_buffer.committed"))
            fin = [e for e in evs if e["type"].endswith(".completed")][0]
            ack = [e for e in evs if e["type"] == "input_audio_buffer.committed"][0]
            assert fin["audio_processed"] > 0.5
            assert ack["audio_processed"] == 0.0
            assert evs.index(fin) < evs.index(ack)


# 7 live partial revision before the final
def test_delta_revision_before_final(client):
    for ws, _ in open_session(client, lang="en"):
        ws.send_bytes(speech_pcm(1.2))
        ws.send_bytes(silence_pcm(1.2))
        evs = drain_until(ws, ("conversation.item.input_audio_transcription.delta",
                               "conversation.item.input_audio_transcription.completed"))
        deltas = [e for e in evs if e["type"].endswith(".delta")]
        assert deltas, "a live partial revision must be emitted during speech"
        for d in deltas:
            assert d["audio_processed"] > 0.0
        comp = [e for e in evs if e["type"].endswith(".completed")]
        assert comp, "final follows the revisions"
        assert evs.index(deltas[-1]) < evs.index(comp[0])


# 8 cancel discards the in-flight decode result (capacity retained until compute ends)
def test_cancel_discards_inflight_result(client_factory):
    m = MockOllama(delay_s=1.5)
    cfg = Config(token="test-token", ollama_url="http://mock-ollama")
    tc = TestClient(appmod.create_app(cfg, lambda: httpx.Client(transport=m.transport(), base_url=cfg.ollama_url)))
    for ws, _ in open_session(tc, lang="en"):
        ws.send_bytes(speech_pcm(1.0))
        ws.send_text(json.dumps({"type": "response.cancel"}))
    assert len(m.requests) == 1


# 9 malformed frames + bounded buffer
def test_unknown_type_and_odd_frame(client):
    for ws, _ in open_session(client, lang="en"):
        ws.send_bytes(b"\x01")
        ws.send_text(json.dumps({"type": "nonsense.type"}))
        ev = json.loads(ws.receive_text())
        assert ev["code"] == "unknown_type"


# 10 endpoint final racing an immediate commit: final must precede ACK
def test_endpoint_final_precedes_commit_ack_when_racing(client):
    for ws, _ in open_session(client, lang="en"):
        ws.send_bytes(speech_pcm(1.5))
        ws.send_bytes(silence_pcm(1.2))
        ws.send_text(json.dumps({"type": "input_audio_buffer.commit"}))
        evs = drain_until(ws, ("input_audio_buffer.committed",))
        fins = [e for e in evs if e["type"].endswith(".completed")]
        ack = [e for e in evs if e["type"] == "input_audio_buffer.committed"][0]
        assert fins, "flushed endpoint final must not be lost"
        assert evs.index(fins[0]) < evs.index(ack)


# 11 word times advance across multiple finals of one epoch
def test_words_advance_across_multiple_finals(client):
    for ws, _ in open_session(client, lang="en"):
        fins = []
        for _ in range(2):
            ws.send_bytes(speech_pcm(1.5))
            ws.send_bytes(silence_pcm(1.2))
            evs = drain_until(ws, ("conversation.item.input_audio_transcription.completed",))
            fins.append([e for e in evs if e["type"].endswith(".completed")][0])
        prev_end = None
        for fin in fins:
            assert fin["words"]
            for w in fin["words"]:
                if prev_end is not None:
                    assert w["start"] >= prev_end - 0.05
                prev_end = max(prev_end or 0.0, w["end"])
            assert fin["words"][-1]["end"] <= fin["audio_processed"] + 0.05


# 12 alignment skipped honestly when not requested
def test_words_not_requested_skips_alignment(client):
    for ws, _ in open_session(client, lang="en", word_timestamps=False):
        ws.send_bytes(speech_pcm(1.5))
        ws.send_bytes(silence_pcm(1.2))
        evs = drain_until(ws, ("conversation.item.input_audio_transcription.completed",))
        fin = [e for e in evs if e["type"].endswith(".completed")][0]
        assert fin["words"] == []
        assert fin["alignment"]["status"] == "not_requested"


# 13 event_id unique across multiple commit epochs on one connection
def test_event_id_unique_across_epochs(client):
    for ws, _ in open_session(client, lang="en"):
        ids = []
        for _ in range(3):
            ws.send_bytes(speech_pcm(1.5))
            ws.send_bytes(silence_pcm(1.2))
            ws.send_text(json.dumps({"type": "input_audio_buffer.commit"}))
            evs = drain_until(ws, ("input_audio_buffer.committed",))
            ids.extend(e["event_id"] for e in evs)
        assert len(ids) == len(set(ids))


def test_segment_overflow_is_explicit(client_factory):
    cfg = Config(token="test-token", ollama_url="http://mock-ollama", buffer_cap_s=2.0)
    tc = client_factory(cfg)
    for ws, _ in open_session(tc, lang="en"):
        chunk = speech_pcm(0.2)
        for _ in range(15):
            ws.send_bytes(chunk)
        evs = drain_until(ws, ("error",))
        assert any(e["code"] == "segment_buffer_overflow" for e in evs)
