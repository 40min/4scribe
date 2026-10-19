"""Finding-driven tests: onset preroll, grouping-equivalent segmentation,
bounded admission/delivery, overflow clock/recovery.

These tests exercise the ORIGINAL service path and would fail on the pre-fix
implementation. Timing-dependent scenarios run against a REAL loopback
uvicorn server (ephemeral port, closed in the test) with the websockets
client; hermetic protocol tests use the TestClient.
"""
import asyncio
import concurrent.futures
import json

import pytest
import uvicorn
import websockets

from tests.conftest import URL, open_session, drain_until, speech_pcm, silence_pcm
from tests.mock_ollama import MockOllama
from fourscribe.config import Config
from fourscribe import app as appmod
from fourscribe.app import BoundedEventQueue
from fastapi.testclient import TestClient
import httpx

RATE = 16000

FREQ_TEXTS = {220: "alpha phrase words", 330: "beta phrase words",
              440: "gamma phrase words"}


def multi_phrase_pcm():
    """Three identifiable phrases (distinct frequencies) separated by >800 ms
    silences, with a final endpointing silence."""
    return (speech_pcm(0.8, freq=220) + silence_pcm(1.0)
            + speech_pcm(0.8, freq=330) + silence_pcm(1.0)
            + speech_pcm(0.8, freq=440) + silence_pcm(0.9))


def feed_variants(pcm):
    yield "20ms", [pcm[i:i + 2 * 320] for i in range(0, len(pcm), 2 * 320)]
    yield "one-large-message", [pcm]
    sizes = []
    for s in (0.3, 0.07, 0.5, 0.02, 1.0, 0.13, 0.44, 0.9, 0.26, 0.61, 0.05, 0.33):
        sizes.append(int(s * RATE * 2))
    chunks, pos = [], 0
    for sz in sizes:
        chunks.append(pcm[pos:pos + sz])
        pos += sz
    if pos < len(pcm):
        chunks.append(pcm[pos:])
    yield "irregular", [c for c in chunks if c]


def finals_of(client, chunks, language="en"):
    out = []
    for ws, _ in open_session(client, lang=language, word_timestamps=True):
        for c in chunks:
            ws.send_bytes(c)
        ws.send_text(json.dumps({"type": "input_audio_buffer.commit"}))
        evs = drain_until(ws, ("input_audio_buffer.committed",), max_events=200)
        out = [e for e in evs if e["type"].endswith(".completed")]
        ack_idx = [i for i, e in enumerate(evs) if e["type"] == "input_audio_buffer.committed"]
        for f in out:
            assert evs.index(f) < ack_idx[0]
    return out


class _Server:
    """Ephemeral loopback uvicorn server for one scenario."""

    def __init__(self, cfg, mock, aligner):
        self.app = appmod.create_app(cfg, lambda: httpx.Client(
            transport=mock.transport(), base_url=cfg.ollama_url), aligner=aligner)
        self.server = uvicorn.Server(uvicorn.Config(
            self.app, host="127.0.0.1", port=0, log_level="error"))

    async def __aenter__(self):
        self.task = asyncio.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.02)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/v1/audio/transcriptions/realtime"
        return self

    async def __aexit__(self, *exc):
        self.server.should_exit = True
        try:
            await asyncio.wait_for(self.task, 5)
        except Exception:  # noqa: BLE001
            self.task.cancel()

    async def session(self, language="en", word_timestamps=True):
        return websockets.connect(self.url, additional_headers={
            "authorization": "Bearer test-token", "max_size": None}), language


async def _run_session(url, language, chunks, commit=True, max_wait=20.0,
                       cancel_after=None):
    """Feed chunks, return all received events until commit ACK (or timeout)."""
    evs = []
    async with websockets.connect(url, additional_headers={
            "authorization": "Bearer test-token", "max_size": None}) as ws:
        await ws.send(json.dumps({"type": "session.update", "session": {
            "sample_rate": 16000, "language": language,
                "endpointing_ms": 800, "word_timestamps": True}}))
        evs.append(json.loads(await ws.recv()))
        t_end = asyncio.get_event_loop().time() + max_wait
        for c in chunks:
            await ws.send(c)
        if cancel_after is not None:
            await asyncio.sleep(cancel_after)
            await ws.send(json.dumps({"type": "response.cancel"}))
        if commit:
            await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        while asyncio.get_event_loop().time() < t_end:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=min(2.0, max(0.1, t_end - asyncio.get_event_loop().time())))
            except asyncio.TimeoutError:
                continue
            except websockets.ConnectionClosed:
                break
            ev = json.loads(raw)
            evs.append(ev)
            if ev["type"] == "input_audio_buffer.committed":
                break
    return evs


# finding 1: onset preroll preserved; timestamps on the original clock
def test_onset_preroll_fully_present_in_decoder_payload(client, mock):
    marker = speech_pcm(0.2, freq=220)          # voiced marker = phrase onset
    waveform = silence_pcm(0.1) + marker + speech_pcm(0.5, freq=330) + silence_pcm(1.0)
    for ws, _ in open_session(client, lang="en"):
        for i in range(0, len(waveform), 2 * 320):  # 20 ms paced frames
            ws.send_bytes(waveform[i:i + 2 * 320])
        evs = drain_until(ws, ("conversation.item.input_audio_transcription.completed",))
    fin = [e for e in evs if e["type"].endswith(".completed")][0]
    # the segment the decoder saw must start EXACTLY at the marker's first
    # sample (run start frame), with the full marker present: no missing
    # leading audio, offsets on the original clock
    expected = waveform[len(silence_pcm(0.1)):len(silence_pcm(0.1)) + len(marker)]
    mock_pcm = mock.pcm_log[-1]
    assert mock_pcm[:len(expected)] == expected
    assert fin["audio_processed"] >= 1.6  # 0.1+0.2+0.5+0.8 close position


# finding 2: grouping-equivalent segmentation
def test_segmentation_identical_across_groupings(client_factory):
    pcm = multi_phrase_pcm()
    ref = None
    for label, chunks in feed_variants(pcm):
        fins = finals_of(client_factory(freq_texts=FREQ_TEXTS), chunks)
        seq = [(f["transcript"], f["audio_processed"],
                tuple((w["word"], w["start"], w["end"]) for w in f["words"]))
               for f in fins]
        assert len(fins) == 3, f"{label}: expected 3 endpoint finals, got {len(fins)}"
        assert [s[0] for s in seq] == ["alpha phrase words", "beta phrase words",
                                       "gamma phrase words"], label
        if ref is None:
            ref = seq
        else:
            assert seq == ref, f"{label}: segmentation differs from 20ms reference"
        for f in fins:
            prev = 0.0
            for w in f["words"]:
                assert 0 <= w["start"] <= w["end"]
                assert w["start"] >= prev - 1e-6
                prev = w["end"]
            assert f["words"][-1]["end"] <= f["audio_processed"] + 0.05


# finding 2b: trailing speech after closures inside one message is not lost
def test_trailing_speech_after_closure_in_one_message(client_factory):
    pcm = multi_phrase_pcm()
    fins = finals_of(client_factory(freq_texts=FREQ_TEXTS), [pcm])
    assert [f["transcript"] for f in fins] == ["alpha phrase words", "beta phrase words",
                                               "gamma phrase words"]
    assert fins[-1]["words"]


# finding 3: bounded finalization admission under a slow decoder (REAL server)
def test_final_backlog_bounded_with_explicit_drop():
    async def scenario():
        mock = MockOllama(delay_s=0.4, freq_texts={220: "alpha phrase words"})
        cfg = Config(token="test-token", ollama_url="http://mock-ollama",
                     max_pending_finals=4)
        async with _Server(cfg, mock, _FakeAligner()) as srv:
            phrases = b"".join(speech_pcm(0.3, freq=220) + silence_pcm(1.0)
                               for _ in range(6))
            return await _run_session(srv.url, "en", [phrases], max_wait=25.0)

    evs = asyncio.run(scenario())
    drops = [e for e in evs if e.get("code") == "final_backlog_overflow"]
    fins = [e for e in evs if e["type"].endswith(".completed")]
    acks = [e for e in evs if e["type"] == "input_audio_buffer.committed"]
    assert drops, "admission overflow must be reported explicitly"
    assert len(fins) + len(drops) == 6
    assert all("dropped audio span" in d["message"] for d in drops)
    for i, ack in enumerate(acks):
        prior = [e for e in evs if e["type"].endswith(".completed")
                 and evs.index(e) < evs.index(ack)]
        assert prior, "finals must precede the commit ACK"


class _FakeAligner:
    name = "fake-aligner"
    available = True

    def align(self, pcm, text, language):
        dur = len(pcm) / (2 * RATE)
        words = text.split()
        if not words or dur <= 0:
            return []
        step = dur / (len(words) + 1)
        return [{"word": w, "start": round(step * i, 3), "end": round(step * (i + 1), 3)}
                for i, w in enumerate(words)]


# finding 3: bounded outbound delivery policy (unit) — HARD bound, explicit overload
def test_bounded_event_queue_policy():
    calls = []
    q = BoundedEventQueue(maxsize=3, on_overload=lambda: calls.append(1))
    for i in range(3):
        q.put({"type": "x.delta", "i": i})
    assert len(q) == 3
    q.put({"type": "x.delta", "i": 3})          # at capacity: stale delta dropped
    assert q.dropped_deltas == 1 and len(q) == 3
    q.put({"type": "y.completed", "i": 4})      # final: evicts oldest delta
    assert len(q) == 3 and q.dropped_deltas == 2
    q.put({"type": "input_audio_buffer.committed", "i": 5})
    q.put({"type": "z.completed", "i": 6})      # evicts the last delta
    assert q.dropped_deltas == 4
    # HARD bound: nothing droppable left -> event NOT appended, overload once
    q.put({"type": "w.completed", "i": 7})
    q.put({"type": "v.completed", "i": 8})
    q.put({"type": "input_audio_buffer.committed", "i": 9})
    assert len(q) == 3, "hard finite bound must hold for non-delta events"
    assert q.overflows == 3
    assert q.overload_fired and calls == [1], "overload callback fires exactly once"
    got = [q._q[i] for i in range(len(q))]
    assert [g["type"] for g in got] == ["y.completed", "input_audio_buffer.committed",
                                        "z.completed"]  # first finals/ACK retained
    # subsequent delta while overloaded: still droppable, no unbounded growth
    q.put({"type": "d.delta", "i": 10})
    assert len(q) == 3 and q.dropped_deltas == 5


# finding 3: cancel discards the admitted backlog cleanly; commit still ACKs
def test_cancel_clears_admitted_backlog():
    async def scenario():
        mock = MockOllama(delay_s=0.4, freq_texts={220: "alpha phrase words"})
        cfg = Config(token="test-token", ollama_url="http://mock-ollama")
        async with _Server(cfg, mock, _FakeAligner()) as srv:
            phrases = b"".join(speech_pcm(0.3, freq=220) + silence_pcm(1.0)
                               for _ in range(6))
            return await _run_session(srv.url, "en", [phrases], max_wait=25.0,
                                      cancel_after=0.05)

    evs = asyncio.run(scenario())
    fins = [e for e in evs if e["type"].endswith(".completed")]
    acks = [e for e in evs if e["type"] == "input_audio_buffer.committed"]
    assert acks, "commit must still ACK after cancel"
    # the endpoint finals of the cancelled generation were voided: no final may
    # arrive after the cancel was sent and before the ACK of the new epoch
    cancel_seen = False
    for e in evs:
        if e["type"] == "response.cancel.notice":
            cancel_seen = True
        elif e["type"].endswith(".completed") and cancel_seen:
            pytest.fail("final of a cancelled generation arrived after cancel")


# finding 4: overflow advances the original clock; recovery has exact offsets
def test_overflow_recovery_exact_original_clock():
    async def scenario():
        mock = MockOllama(delay_s=0.02, freq_texts={220: "alpha phrase words",
                                                    330: "beta phrase words"})
        cfg = Config(token="test-token", ollama_url="http://mock-ollama",
                     buffer_cap_s=2.0)
        async with _Server(cfg, mock, _FakeAligner()) as srv:
            dropped = speech_pcm(2.5, freq=220)         # triggers overflow (cap 2.0)
            recovery = speech_pcm(0.8, freq=330) + silence_pcm(1.0)
            chunks = [dropped] + [recovery[i:i + 2 * 1600]
                                  for i in range(0, len(recovery), 2 * 1600)]
            return await _run_session(srv.url, "en", chunks, max_wait=20.0)

    evs = asyncio.run(scenario())
    overflows = [e for e in evs if e.get("code") == "segment_buffer_overflow"]
    assert overflows, "overflow must be explicit"
    fins = [e for e in evs if e["type"].endswith(".completed")]
    assert fins, "recovered phrase must finalize"
    fin = fins[0]
    assert fin["transcript"] == "beta phrase words"     # no stale buffer mixing
    # original clock: 2.5 s dropped + 0.8 s recovery speech + 0.8 s hangover
    # (the endpoint closes mid-silence, before the commit) = 4.1 s
    assert abs(fin["audio_processed"] - 4.1) < 0.06
    for w in fin["words"]:
        assert w["start"] >= 2.5 - 0.05                 # after the dropped span
        assert w["end"] <= fin["audio_processed"] + 0.05


# ---------- correction round 2 ----------

# finding 1 (round 2): HARD outgoing bound under a flooded, non-reading client
def test_flood_with_stalled_client_closes_session_explicitly():
    big_text = "alpha phrase words " * 80     # large finals fill TCP quickly

    async def scenario():
        mock = MockOllama(delay_s=0.02, freq_texts={220: big_text})
        cfg = Config(token="test-token", ollama_url="http://mock-ollama",
                     outbound_queue_max=8)
        async with _Server(cfg, mock, _FakeAligner()) as srv:
            evs, closed = [], False
            loop = asyncio.get_event_loop()
            async with websockets.connect(srv.url, additional_headers={
                    "authorization": "Bearer test-token", "max_size": None}) as ws:
                await ws.send(json.dumps({"type": "session.update", "session": {
                    "sample_rate": 16000, "language": "en",
                    "endpointing_ms": 800, "word_timestamps": True}}))
                # do NOT read: the client stops consuming; the pump stalls once
                # kernel buffers fill, the outbound queue then hits its hard bound
                for _ in range(500):
                    await ws.send(speech_pcm(0.3, freq=220) + silence_pcm(1.0))
                    await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                t_end = loop.time() + 25
                while loop.time() < t_end and not closed:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=2)
                        evs.append(json.loads(raw))
                    except asyncio.TimeoutError:
                        continue
                    except websockets.ConnectionClosed:
                        closed = True
                        break
            return evs, closed

    evs, closed = asyncio.run(scenario())
    assert closed, "hard outbound overflow must close the session explicitly"
    # the server stopped delivering: the delivered prefix is strictly finite
    assert len(evs) < 2 * 500, "delivery must stop at the hard bound, not drain all"
    # whatever was delivered kept the ordering contract (no ACK before finals)
    fin_idx = [i for i, e in enumerate(evs) if e["type"].endswith(".completed")]
    ack_idx = [i for i, e in enumerate(evs) if e["type"] == "input_audio_buffer.committed"]
    if fin_idx and ack_idx:
        assert fin_idx[0] < ack_idx[0]


# finding 2 (round 2): partial cadence is NOT reset by continuation frames
def test_partial_cadence_not_reset_by_continuation(monkeypatch):
    import fourscribe.session as sess
    from types import SimpleNamespace
    fake = {"t": 100.0}
    monkeypatch.setattr(sess, "monotonic", lambda: fake["t"])

    class _Asr:
        def __init__(self):
            self.calls = []

        def decode(self, pcm, language=None, num_predict=None):
            self.calls.append((fake["t"], len(pcm)))
            return SimpleNamespace(text="hello words here")

    async def scenario():
        cfg = Config(token="t", ollama_url="http://mock", partial_interval_s=4.0)
        asr = _Asr()
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        evs = []
        s = sess.RealtimeSession(cfg, asr, _FakeAligner(), pool, evs.append,
                                 align_pool=pool)
        s.word_timestamps_requested = False
        await s.handle_session_update({"session": {"language": "en",
                                                   "word_timestamps": False}})
        evs.clear()
        msg = speech_pcm(0.1, freq=220)
        for _ in range(100):  # 10 s of continuous speech, 100 ms per message
            fake["t"] += 0.1
            await s.handle_binary(msg)
            await asyncio.sleep(0.01)  # let the instant decode settle
        for _ in range(100):
            await asyncio.sleep(0.01)
        fake["t"] += 5.0
        await s.handle_binary(silence_pcm(1.0))  # endpoint -> final
        await s.handle_commit()
        for _ in range(100):
            await asyncio.sleep(0.01)
        return asr, evs

    asr, evs = asyncio.run(scenario())
    # cadence (interval 4 s): partials at ~0.8/4.8/8.8 s after onset; the LAST
    # decode is the endpoint FINAL of the larger closing buffer. The pre-fix
    # code reset the deadline on EVERY continuation frame and produced one
    # partial per message (~92+ decodes).
    partials = [c for c in asr.calls if c[0] < 112.0]   # before the endpoint close
    assert 1 <= len(partials) <= 3, f"cadence violated: {asr.calls}"
    assert partials[0][0] <= 101.0, f"first partial not prompt: {asr.calls}"
    for prev, cur in zip(partials, partials[1:]):
        assert cur[0] - prev[0] >= 3.9, f"partials closer than the interval: {asr.calls}"
    # finals stay prompt and ordered: final before ACK, and the final decode
    # happened promptly after speech ended (not queued behind the cadence)
    fins = [e for e in evs if e["type"].endswith(".completed")]
    acks = [e for e in evs if e["type"] == "input_audio_buffer.committed"]
    assert fins and acks
    assert evs.index(fins[0]) < evs.index(acks[0])


# finding 2 (round 2): one in-flight/queued partial per session; slow decoder
def test_single_inflight_partial_with_slow_decoder(monkeypatch):
    import fourscribe.session as sess
    from types import SimpleNamespace
    import threading
    fake = {"t": 100.0}
    monkeypatch.setattr(sess, "monotonic", lambda: fake["t"])
    gate = threading.Event()

    class _SlowAsr:
        def __init__(self):
            self.calls = []

        def decode(self, pcm, language=None, num_predict=None):
            self.calls.append(fake["t"])
            gate.wait(5)
            return SimpleNamespace(text="hello words here")

    async def scenario():
        cfg = Config(token="t", ollama_url="http://mock", partial_interval_s=4.0)
        asr = _SlowAsr()
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        s = sess.RealtimeSession(cfg, asr, _FakeAligner(), pool, lambda e: None,
                                 align_pool=pool)
        s.word_timestamps_requested = False
        await s.handle_session_update({"session": {"language": "en",
                                                   "word_timestamps": False}})
        msg = speech_pcm(0.1, freq=220)
        for _ in range(20):  # reach the first partial (>= 0.8 s buffered)
            fake["t"] += 0.1
            await s.handle_binary(msg)
            await asyncio.sleep(0.01)
        assert len(asr.calls) == 1, asr.calls
        for _ in range(300):  # 30 s more speech while the decode is stalled
            fake["t"] += 0.1
            await s.handle_binary(msg)
            await asyncio.sleep(0.005)
        assert len(asr.calls) == 1, "second partial admitted while one in flight"
        gate.set()
        for _ in range(100):
            await asyncio.sleep(0.01)
        n_after_release = len(asr.calls)
        for _ in range(10):  # cadence continues from the pending deadline
            fake["t"] += 0.1
            await s.handle_binary(msg)
            await asyncio.sleep(0.005)
        for _ in range(100):
            await asyncio.sleep(0.01)
        return asr, n_after_release

    asr, n_after_release = asyncio.run(scenario())
    assert n_after_release <= 2, f"cadence not honored after release: {asr.calls}"


# finding 2 (round 2): no obsolete partial across a commit/epoch boundary
def test_no_obsolete_partial_across_epoch_boundary(monkeypatch):
    import fourscribe.session as sess
    from types import SimpleNamespace
    fake = {"t": 100.0}
    monkeypatch.setattr(sess, "monotonic", lambda: fake["t"])

    class _CrossingAsr:
        """First decode simulates a concurrent commit completing mid-decode."""
        def __init__(self):
            self.session = None
            self.calls = 0

        def decode(self, pcm, language=None, num_predict=None):
            self.calls += 1
            if self.calls == 1:
                self.session.epoch_start_samples = self.session.epoch_samples + 1
            return SimpleNamespace(text="hello words here")

    async def scenario():
        cfg = Config(token="t", ollama_url="http://mock")
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        evs = []
        asr = _CrossingAsr()
        s = sess.RealtimeSession(cfg, asr, _FakeAligner(), pool,
                                 evs.append, align_pool=pool)
        asr.session = s
        s.word_timestamps_requested = False
        await s.handle_session_update({"session": {"language": "en",
                                                   "word_timestamps": False}})
        evs.clear()
        fake["t"] += 0.1
        await s.handle_binary(speech_pcm(1.0, freq=220))
        for _ in range(200):
            await asyncio.sleep(0.005)
        return evs

    evs = asyncio.run(scenario())
    deltas = [e for e in evs if e["type"].endswith(".delta")]
    assert not deltas, "a partial whose epoch boundary moved must not be emitted"
