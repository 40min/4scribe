"""Opt-in correction contracts: raw delivery, shared capacity and invalidation."""
import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
import httpx
from fastapi.testclient import TestClient

from fourscribe.app import BoundedEventQueue
from fourscribe.config import Config
from fourscribe.enhancement import EnhanceError, TextEnhancer
from fourscribe.session import RealtimeSession
from tests.conftest import FakeAligner, speech_pcm
from tests.test_mobile_batch import (make_app, fake_converter, audio_files, PATH,
                                     AUTH, MOBILE_TOKEN, WS_TOKEN, DeclineVerifier)

ENH_TOKEN = "enhancement-text-000000"
ENHANCED = "conversation.item.input_audio_transcription.enhanced"


def config(**kwargs):
    return Config(token=WS_TOKEN, mobile_token=MOBILE_TOKEN,
                  enhancement_token=ENH_TOKEN, **kwargs)


@pytest.mark.parametrize("mode", ["off", "corrected", "unchanged", "enhancement_busy",
                                  "enhancement_timeout", "enhancement_unavailable",
                                  "input_too_large"])
def test_batch_optional_result_and_asr_slot(monkeypatch, mode):
    app, _, _ = make_app(monkeypatch, cfg=config(), converter=fake_converter())
    batch = app.state.batch_transcriber
    calls = []

    async def correct(text, language):
        calls.append((text, language))
        assert not batch.decode_sem.locked(), "correction must release ASR capacity"
        if mode.startswith("enhancement_") or mode == "input_too_large":
            raise EnhanceError(mode)
        return {"status": mode, "enhanced_text": text + "." if mode == "corrected" else None}

    monkeypatch.setattr(app.state.text_enhancer, "correct", correct)
    with TestClient(app) as client:
        response = client.post(PATH, headers=AUTH,
                               files={**audio_files(), "enhance": (None, "false" if mode == "off" else "true")})
    assert response.status_code == 200
    payload = response.json()
    assert payload["text"]
    assert len(calls) == (mode != "off")
    assert payload["enhanced_text"] == (payload["text"] + "." if mode == "corrected" else None)
    assert payload["enhancer_error"] == (mode.startswith("enhancement_") or mode == "input_too_large")
    assert isinstance(payload["enhancer_ms"], (float, int)) and payload["enhancer_ms"] >= 0


@pytest.mark.parametrize("flag", ["yes", "TRUE", "1", ""])
def test_batch_invalid_flag(monkeypatch, flag):
    app, mock, _ = make_app(monkeypatch, converter=fake_converter())
    with TestClient(app) as client:
        response = client.post(PATH, headers=AUTH,
                               files={**audio_files(), "enhance": (None, flag)})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_enhance"
    assert not mock.requests


def test_batch_query_fallback_and_blank_skip(monkeypatch):
    app, _, _ = make_app(monkeypatch, converter=fake_converter(), verifier=DeclineVerifier())

    async def forbidden(*args):
        pytest.fail("verifier-declined text must not be enhanced")

    monkeypatch.setattr(app.state.text_enhancer, "correct", forbidden)
    with TestClient(app) as client:
        response = client.post(PATH + "?enhance=true", headers=AUTH, files=audio_files())
    assert response.json()["text"] == ""
    assert response.json()["enhancer_error"] is False


class BlockingEnhancer:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = []

    async def correct(self, text, language):
        self.calls.append((text, language))
        self.started.set()
        # Deliberately resist wrapper cancellation: generation guard still wins.
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            try:
                await asyncio.wait_for(self.release.wait(), 0.1)
            except TimeoutError:
                pass
        return {"status": "corrected", "enhanced_text": text + "."}


def session(pool, sink, enhancer):
    cfg = config()
    instance = RealtimeSession(cfg, None, FakeAligner(), pool, sink, enhancer=enhancer)
    instance.language = "ru"
    instance.word_timestamps_requested = True

    async def decode(*args, **kwargs):
        return "raw words"

    instance.run_decode = decode
    return instance


@pytest.mark.parametrize("invalidate", [None, "disable", "cancel", "close"])
def test_raw_final_ack_immediate_words_and_late_suppression(invalidate):
    async def run():
        received = []
        enhancer = BlockingEnhancer()
        with ThreadPoolExecutor(1) as pool:
            sess = session(pool, received.append, enhancer)
            await sess.handle_session_update({"session": {"enhance": True, "word_timestamps": True}})
            received.clear()
            pcm = speech_pcm(1)
            await asyncio.wait_for(sess._finalize(pcm, 16000, 0, True), 1)
            assert [event["type"].rsplit(".", 1)[-1] for event in received] == ["completed", "committed"]
            raw = received[0]
            original_words = list(raw["words"])
            assert [word["word"] for word in original_words] == ["raw", "words"]
            await asyncio.wait_for(enhancer.started.wait(), 1)
            await sess.handle_session_update({"session": {"language": "en"}})
            assert sess.enhance is True
            if invalidate == "disable":
                await sess.handle_session_update({"session": {"enhance": False}})
            elif invalidate == "cancel":
                await sess.handle_cancel()
            elif invalidate == "close":
                await sess.close()
            enhancer.release.set()
            if sess._enhancement_tasks:
                await asyncio.wait_for(asyncio.gather(*sess._enhancement_tasks), 1)
            corrections = [event for event in received if event["type"] == ENHANCED]
            assert len(corrections) == (invalidate is None)
            assert enhancer.calls == [("raw words", "ru")]
            if corrections:
                ev = corrections[0]
                assert ev["segment_id"] == raw["segment_id"]
                assert ev["event_id"] != raw["event_id"]
                assert ev["enhanced_text"] == "raw words."
                assert not ({"words", "audio_processed", "transcript"} & ev.keys())
            assert raw["words"] == original_words
            await sess.close()
    asyncio.run(run())


def test_ws_strict_flag_and_ids_across_commits():
    async def run():
        received = []
        with ThreadPoolExecutor(1) as pool:
            sess = session(pool, received.append, None)
            await sess.handle_session_update({"session": {"enhance": "true"}})
            assert received[-1]["error"]["code"] == "invalid_enhance"
            assert received[-1]["applied"]["enhance"] is False
            for _ in range(2):
                await sess._finalize(speech_pcm(1), 16000, 0, True)
            finals = [ev for ev in received if ev["type"].endswith(".completed")]
            assert len(finals) == 2
            assert finals[0]["segment_id"] != finals[1]["segment_id"]
            assert not sess._enhancement_tasks
            await sess.close()
    asyncio.run(run())


def test_optional_pressure_does_not_evict_raw_or_close_and_can_be_purged():
    async def run():
        overloaded = []
        queue = BoundedEventQueue(2, lambda: overloaded.append(True))
        raw = {"type": "conversation.item.input_audio_transcription.completed"}
        ack = {"type": "input_audio_buffer.committed"}
        optional = {"type": ENHANCED}
        queue.put(raw)
        queue.put(ack)
        for _ in range(50):
            queue.put(optional)
        assert len(queue) == 2 and not overloaded and queue.dropped_enhancements == 50
        assert await queue.get() is raw
        queue.put(optional)
        queue.put(raw)
        assert await queue.get() is ack
        assert await queue.get() is raw
        queue.put(optional)
        queue.discard_enhancements()
        assert len(queue) == 0 and not overloaded
    asyncio.run(run())


def test_shared_engine_slot_retained_after_internal_cancellation(monkeypatch):
    async def run():
        engine = TextEnhancer(config())
        started, release = asyncio.Event(), asyncio.Event()

        async def correct(text, timeout):
            started.set()
            await release.wait()
            return text + "."

        monkeypatch.setattr(engine, "_ollama_correct", correct)
        task = asyncio.create_task(engine.correct("raw"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(EnhanceError, match="enhancement_busy"):
            await engine.correct("second")
        release.set()
        await asyncio.gather(*engine._retained)
        assert (await engine.correct("second"))["enhanced_text"] == "second."
        await engine.close()
    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["unchanged", "enhancement_busy", "enhancement_timeout",
                                      "enhancement_unavailable", "input_too_large"])
def test_ws_optional_outcomes(outcome):
    async def run():
        received = []

        class Enhancer:
            async def correct(self, text, language):
                if outcome != "unchanged":
                    raise EnhanceError(outcome)
                return {"status": "unchanged", "enhanced_text": None}

        with ThreadPoolExecutor(1) as pool:
            sess = session(pool, received.append, Enhancer())
            await sess.handle_session_update({"session": {"enhance": True}})
            await sess._finalize(speech_pcm(1), 16000, 0, True)
            await asyncio.gather(*sess._enhancement_tasks)
            ev = received[-1]
            assert ev["type"] == ENHANCED
            assert ev["enhanced_text"] is None
            assert ev["enhancer_error"] == (outcome != "unchanged")
            assert ev["status"] == ("unchanged" if outcome == "unchanged" else "error")
            assert ev["enhancer_ms"] >= 0
            if outcome != "unchanged":
                assert ev["error"]["code"] == outcome
            await sess.close()
    asyncio.run(run())


def test_http_batch_ws_share_one_engine_admission(monkeypatch):
    async def run():
        app, _, _ = make_app(monkeypatch, cfg=config(), converter=fake_converter())
        engine = app.state.text_enhancer
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def upstream(text, timeout):
            calls.append(text)
            started.set()
            await release.wait()
            return text + "."

        monkeypatch.setattr(engine, "_ollama_correct", upstream)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://testserver") as client:
                standalone = asyncio.create_task(client.post(
                    "/v1/text/enhance", headers={"authorization": "Bearer " + ENH_TOKEN},
                    json={"text": "standalone"}))
                await asyncio.wait_for(started.wait(), 1)
                batch = await client.post(PATH + "?enhance=true", headers=AUTH,
                                          files=audio_files())
                assert batch.status_code == 200
                assert batch.json()["text"] and batch.json()["enhancer_error"] is True
                received = []
                with ThreadPoolExecutor(1) as pool:
                    sess = session(pool, received.append, engine)
                    await sess.handle_session_update({"session": {"enhance": True}})
                    await sess._finalize(speech_pcm(1), 16000, 0, True)
                    await asyncio.gather(*sess._enhancement_tasks)
                    assert received[-1]["error"]["code"] == "enhancement_busy"
                    assert calls == ["standalone"]
                    await sess.close()
                release.set()
                assert (await standalone).status_code == 200
    asyncio.run(run())


def test_session_admission_has_no_queue_and_cancel_purges_optional_events():
    async def run():
        queue = BoundedEventQueue(10)
        enhancer = BlockingEnhancer()
        with ThreadPoolExecutor(1) as pool:
            sess = session(pool, queue.put, enhancer)
            sess._discard_enhancements = queue.discard_enhancements
            await sess.handle_session_update({"session": {"enhance": True}})
            for index in range(3):
                await sess._finalize(speech_pcm(1), (index + 1) * 16000, index * 16000, True)
            assert len(sess._enhancement_tasks) == 1
            assert sum(event["type"] == ENHANCED for event in queue._q) == 2
            await enhancer.started.wait()
            await sess.handle_cancel()
            assert not any(event["type"] == ENHANCED for event in queue._q)
            enhancer.release.set()
            await asyncio.gather(*sess._enhancement_tasks)
            assert not any(event["type"] == ENHANCED for event in queue._q)
            assert sum(event["type"].endswith(".completed") for event in queue._q) == 3
            await sess.close()
    asyncio.run(run())


@pytest.mark.parametrize("capacity", [3, 8])
def test_queued_corrections_yield_to_raw_events_at_any_capacity(capacity):
    async def run():
        overloaded = []
        queue = BoundedEventQueue(capacity, lambda: overloaded.append(True))
        correction1 = {"type": ENHANCED, "segment_id": "old1"}
        correction2 = {"type": ENHANCED, "segment_id": "old2"}
        final1 = {"type": "conversation.item.input_audio_transcription.completed", "segment_id": "raw1"}
        ack = {"type": "input_audio_buffer.committed"}
        final2 = {"type": "conversation.item.input_audio_transcription.completed", "segment_id": "raw2"}
        for event in (correction1, correction2, final1, ack, final2):
            queue.put(event)
            assert len(queue) <= capacity
        assert not overloaded
        assert [await queue.get() for _ in range(3)] == [final1, ack, final2]
        if capacity == 8:
            assert [await queue.get(), await queue.get()] == [correction1, correction2]
        else:
            assert len(queue) == 0
            assert queue.dropped_enhancements == 2
    asyncio.run(run())


def test_real_websocket_pump_sends_raw_and_ack_before_pending_correction(monkeypatch):
    # All synchronous emits happen in the receive handler before the actual
    # application pump can run. This deterministically queues a correction
    # first, then two raw final/ACK pairs below capacity.
    async def scripted_update(sess, payload):
        sess.emit({"type": ENHANCED, "segment_id": "old"})
        for index in range(2):
            sess.emit({"type": "conversation.item.input_audio_transcription.completed",
                       "segment_id": str(index)})
            sess.emit({"type": "input_audio_buffer.committed", "index": index})

    monkeypatch.setattr(RealtimeSession, "handle_session_update", scripted_update)
    app, _, _ = make_app(monkeypatch, cfg=config(outbound_queue_max=8))
    with TestClient(app) as client:
        with client.websocket_connect(
                "/v1/audio/transcriptions/realtime",
                headers={"authorization": "Bearer " + WS_TOKEN}) as ws:
            ws.send_json({"type": "session.update", "session": {"language": "ru"}})
            received = [ws.receive_json() for _ in range(5)]
    assert [(event["type"].rsplit(".", 1)[-1], event.get("segment_id", event.get("index")))
            for event in received] == [
                ("completed", "0"), ("committed", 0),
                ("completed", "1"), ("committed", 1), ("enhanced", "old")]
