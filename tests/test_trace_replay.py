"""Bounded trace & replay tests (owner assignment 2026-10-17, fresh session;
revised after independent review).

Covers:
1. lifecycle_trace ALLOWLIST sanitization (unknown fields/stages/invalid enums/
   non-finite floats dropped), bounded FIFO, unique per-connection trace files
   with concurrent two-connection tracing (no shared overwrite), flush mutation
   counter (multiple ring wraps + final flush);
2. the ACTUAL frozen Loqui RemotePipeline semantics through the reusable
   replay seam: healthy streaming acceptance; failure NEGATIVE case
   (commit_ack_timeout -> restart without replay); EPOCH-CORRECT attribution
   (patient restarted generation 2 never inherits generation-1 losses and
   later restarts never re-attribute); per-epoch sample mapping after
   reconnect; complete final-coverage accounting beyond the capped report
   sample; speed validation; preserved final short WAV frame;
3. the thin CLI WAV/PCM parsing and token handling (RAM only).

Privacy: synthetic/public input only; reports contain no text/token/audio.
"""
import importlib.util
import asyncio
import io as _io
import json
import math
import os
import socket
import sys
import threading
import time
from collections import deque
from pathlib import Path

import httpx
import pytest
import uvicorn
import websockets

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from fourscribe import app as appmod  # noqa: E402
from fourscribe.config import Config  # noqa: E402
from fourscribe.lifecycle_trace import (  # noqa: E402
    TRACE_STAGES, SessionTracer, new_session_tracer)
from tests.conftest import FakeAligner, silence_pcm, speech_pcm  # noqa: E402
from tests.mock_ollama import MockOllama  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "streaming_replay", ROOT / "deployment" / "replay" / "streaming_replay.py")
sr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sr)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def make_frames(timeline, freq=260.0, amp=12000.0):
    """20 ms frames of (t_s, pcm): 's' = silence seconds, else speech seconds."""
    frames = []
    t = 0.0
    for kind, dur in timeline:
        pcm = silence_pcm(dur) if kind == "s" else speech_pcm(
            dur, amp=amp, freq=freq)
        for i in range(0, len(pcm) - 1, 640):
            frames.append((t, pcm[i:i + 640]))
            t += 0.02
    return frames


def loopback_service(mock, token, endpointing=400, aligner="unavailable"):
    """Real app on an ephemeral loopback port (same pattern as test_commit_empty).

    Default aligner: UNAVAILABLE — word-extent coverage is NOT a coverage
    oracle (Codex finding: a fake aligner ends its last word at n/(n+1) of
    the buffer), so replay speech-coverage tests rely on the full
    audio_processed span, which the client uses when words are absent."""
    cfg = Config(token=token, ollama_url="http://mock-ollama",
                 min_endpointing_ms=endpointing)
    if aligner == "unavailable":
        from fourscribe.aligner import UnavailableAligner
        al = UnavailableAligner()
    else:
        al = FakeAligner()
    app = appmod.create_app(
        cfg, lambda: httpx.Client(transport=mock.transport(),
                                  base_url=cfg.ollama_url), aligner=al)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started
    url = f"ws://127.0.0.1:{port}/4scribe/v1/audio/transcriptions/realtime"
    return server, url


# ---------------- allowlist sanitization + bounded FIFO ----------------------

def test_trace_disabled_by_default_and_factory_none():
    import os
    saved = os.environ.pop("FSCRIBE_LIFECYCLE_TRACE", None)
    try:
        assert new_session_tracer() is None
    finally:
        if saved is not None:
            os.environ["FSCRIBE_LIFECYCLE_TRACE"] = saved


def test_allowlist_rejects_unknown_fields_stages_and_bad_values():
    t = SessionTracer()  # memory-only
    t.record("pcm_accept", abs_start_samp=1, abs_end_samp=2, n=3,
             token="SECRET", note="unknown field with sentinel",
             transcript="SECRET", pcm=b"audio")
    t.record("bogus_stage", anything="should_not_log")       # unknown stage
    t.record("final_emit", job_id="aaaa", span_end_s=float("inf"),
             span_start_s=float("nan"), align_status="not-an-enum")
    t.record("decode_error", job_id="aaaa", err_kind="has space; not identifier")
    t.record("job_admit", job_id="not-hex ZZ", job_kind="bogus",
             abs_start_samp="1", abs_end_samp="2")
    t.record("decode_submit", job_id="deadbeef", partial=True,
             extra="dropped")
    t.record("session_close", n_frames=7, n_bogus_stat=9)
    recs = t.snapshot()
    kinds = [r["kind"] for r in recs]
    assert "bogus_stage" not in kinds            # unknown stage refused
    assert "bogus_stage" not in TRACE_STAGES
    flat = json.dumps(recs)
    assert "SECRET" not in flat and "audio" not in flat and "dropped" not in flat
    assert "should_not_log" not in flat
    assert "not-an-enum" not in flat and "has space" not in flat
    assert "not-hex" not in flat and "bogus" not in flat and "n_bogus" not in flat
    pcm_accept = next(r for r in recs if r["kind"] == "pcm_accept")
    assert set(pcm_accept) == {"conn_id", "t_rel", "kind",
                               "abs_start_samp", "abs_end_samp", "n"}
    fe = next(r for r in recs if r["kind"] == "final_emit")
    assert set(fe) == {"conn_id", "t_rel", "kind", "job_id"}  # bad values dropped
    de = next(r for r in recs if r["kind"] == "decode_error")
    assert set(de) == {"conn_id", "t_rel", "kind", "job_id"}  # err_kind rejected
    ja = next(r for r in recs if r["kind"] == "job_admit")
    assert set(ja) == {"conn_id", "t_rel", "kind"}           # all fields invalid
    ds = next(r for r in recs if r["kind"] == "decode_submit")
    assert set(ds) == {"conn_id", "t_rel", "kind", "job_id", "partial"}
    sc = next(r for r in recs if r["kind"] == "session_close")
    assert set(sc) == {"conn_id", "t_rel", "kind", "n_frames"}


def test_bounded_fifo_and_mutation_counter_flush(tmp_path):
    path = str(tmp_path / "t" / "trace.jsonl")
    t = SessionTracer(file_dir_or_path=path, max_records=2, flush_interval_s=0.05)
    assert str(t.file_path).endswith(".jsonl") and t.file_path != str(
        tmp_path / "t" / "trace.jsonl")  # unique per-connection path
    t.record("pcm_accept", abs_start_samp=0, abs_end_samp=320, n=160)
    t.flush_now()
    with open(t.file_path, encoding="utf-8") as f:
        lines = [json.loads(l) for l in f if l.strip()]
    assert lines[0]["records_n"] == 1

    # three wraps: ring keeps only the newest two, file follows the CURRENT
    # mutation state (mutation counter, not stale length comparison)
    for i in range(1, 6):
        t.record("job_admit", job_id=f"{i:08x}", job_kind="final",
                 abs_start_samp=i * 320, abs_end_samp=(i + 1) * 320)
    t.flush_now()
    with open(t.file_path, encoding="utf-8") as f:
        lines = [json.loads(l) for l in f if l.strip()]
    recs = lines[1:]
    snap = t.snapshot()
    assert len(snap) == 2 and t.evicted == 4
    assert [r["job_id"] for r in recs] == [r["job_id"] for r in snap]
    # another wrap THEN flush: file must update again (finding 2 regression)
    for i in range(6, 9):
        t.record("job_admit", job_id=f"{i:08x}", job_kind="final",
                 abs_start_samp=i * 320, abs_end_samp=(i + 1) * 320)
    t.flush_now()
    with open(t.file_path, encoding="utf-8") as f:
        lines = [json.loads(l) for l in f if l.strip()]
    assert [r["job_id"] for r in lines[1:]] == \
        [r["job_id"] for r in t.snapshot()]
    assert lines[0]["evicted_by_bound"] == 7
    t.close()
    assert not Path(t.file_path + ".tmp").exists()


def test_tracer_flush_failure_never_raises(tmp_path):
    bad = SessionTracer(file_dir_or_path=str(tmp_path / "missing-dir" / "x.jsonl"))
    bad.record("pcm_accept", abs_start_samp=0, abs_end_samp=320, n=160)
    bad.flush_now()
    bad.close()
    # unusable directory fell back to per-connection file or memory-only; the
    # session itself is unaffected either way
    assert bad.file_path is None or str(bad.file_path).endswith(".jsonl")


# ---------------- concurrent per-connection trace files ----------------------

def _trace_ws(url, token, speech, silence_s, flags, label):
    async def run():
        async with websockets.connect(
                url, open_timeout=10,
                additional_headers={"authorization": "Bearer " + token}) as ws:
            await ws.send(json.dumps({"type": "session.update", "session": {
                "sample_rate": 16000, "language": "ru",
                "endpointing_ms": 400, "word_timestamps": True}}))
            await ws.recv()
            await ws.send(speech)
            await asyncio.sleep(silence_s)
            await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
            await asyncio.sleep(silence_s)
            flags[label] = True
    return run()


def test_two_concurrent_connections_get_unique_trace_files(monkeypatch, tmp_path):
    base = str(tmp_path / "tracedir" / "trace.jsonl")
    monkeypatch.setenv("FSCRIBE_LIFECYCLE_TRACE", "1")
    monkeypatch.setenv("FSCRIBE_LIFECYCLE_TRACE_FILE", base)

    mock = MockOllama(delay_s=0.05)
    server, url = loopback_service(mock, "trace-token")
    flags = {}
    try:
        async def main():
            await asyncio.gather(
                _trace_ws(url, "trace-token", speech_pcm(1.0), 0.7, flags, "a"),
                _trace_ws(url, "trace-token", speech_pcm(1.0, freq=340), 0.7,
                          flags, "b"))
        asyncio.run(main())
    finally:
        server.should_exit = True
    time.sleep(0.4)  # writer flush ticks
    files = sorted((tmp_path / "tracedir").glob("trace-*.jsonl"))
    assert len(files) == 2, [p.name for p in files]
    conn_ids = set()
    for p in files:
        raw = p.read_text(encoding="utf-8")
        lines = [json.loads(l) for l in raw.strip().splitlines() if l.strip()]
        header, recs = lines[0], lines[1:]
        ids = {r["conn_id"] for r in recs}
        assert len(ids) == 1                       # single connection per file
        conn_ids |= ids
        assert header["conn_id"] == recs[0]["conn_id"]
        kinds = {r["kind"] for r in recs}
        assert {"pcm_accept", "final_emit", "commit_ack"} <= kinds
        assert header["evicted_by_bound"] == 0
    assert len(conn_ids) == 2                      # both streams retained
    assert not list((tmp_path / "tracedir").glob("*.tmp"))


# ---------------- app trace: redaction, bounds, stage chain ------------------

def test_app_trace_redaction_bounds_and_stage_chain(monkeypatch, tmp_path):
    trace_file = tmp_path / "lifecycle.jsonl"
    monkeypatch.setenv("FSCRIBE_LIFECYCLE_TRACE", "1")
    monkeypatch.setenv("FSCRIBE_LIFECYCLE_TRACE_FILE", str(trace_file))
    monkeypatch.setenv("FSCRIBE_LIFECYCLE_TRACE_MAX", "400")

    mock = MockOllama(delay_s=0.05)
    server, url = loopback_service(mock, "trace-token")
    try:
        events = []

        async def run():
            async with websockets.connect(
                    url, open_timeout=10,
                    additional_headers={"authorization": "Bearer trace-token"}) as ws:
                await ws.send(json.dumps({"type": "session.update", "session": {
                    "sample_rate": 16000, "language": "ru",
                    "endpointing_ms": 400, "word_timestamps": True}}))
                assert json.loads(await ws.recv())["type"] == "session.updated"

                async def reader():
                    try:
                        while True:
                            events.append(json.loads(await ws.recv()))
                    except websockets.ConnectionClosed:
                        pass
                r = asyncio.create_task(reader())
                try:
                    await ws.send(speech_pcm(1.2))
                    await asyncio.sleep(1.0)
                    await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                    await asyncio.sleep(1.0)
                finally:
                    r.cancel()
        asyncio.run(run())
    finally:
        server.should_exit = True

    time.sleep(0.4)  # writer flush ticks
    trace_files = sorted(tmp_path.glob("lifecycle-*.jsonl"))
    assert len(trace_files) == 1, [p.name for p in trace_files]
    trace_file = trace_files[0]
    raw = trace_file.read_text(encoding="utf-8")
    lines = [json.loads(l) for l in raw.strip().splitlines() if l.strip()]
    header, recs = lines[0], lines[1:]
    assert header["trace"] == "fscribe-lifecycle" and header["records_n"] > 0
    kinds = [r["kind"] for r in recs]
    # metadata-only: redaction invariants over the WHOLE bounded file
    assert "Финальн" not in raw and "Тестовый" not in raw
    assert "trace-token" not in raw.lower()
    rels = [r["t_rel"] for r in recs]
    assert rels == sorted(rels)

    for want in ["pcm_accept", "seg_open", "job_admit", "job_start",
                 "decode_submit", "decode_end", "final_emit", "commit_ack"]:
        assert want in kinds, want
    finals = [r for r in recs if r["kind"] == "final_emit"]
    accepts = [r for r in recs if r["kind"] == "pcm_accept"]
    assert finals and accepts
    assert finals[-1]["span_end_s"] >= 0.9
    assert max(a["abs_end_samp"] for a in accepts) >= 16000
    assert any(r["kind"] == "commit_ack" for r in recs)
    assert any(e["type"].endswith(".completed") for e in events)


# ---------------- actual RemotePipeline replay semantics ---------------------

def _run_replay(monkeypatch, timeline, mock_delay, ack_grace, hangover,
                speed, frames=None, token="replay-token", feed_hook=None):
    monkeypatch.setenv("LOQUI_REMOTE_COMMIT_HANGOVER_MS", str(hangover))
    monkeypatch.setenv("LOQUI_REMOTE_COMMIT_ACK_GRACE_SECONDS", str(ack_grace))
    mock = MockOllama(delay_s=mock_delay)
    server, url = loopback_service(mock, token)
    frames = frames if frames is not None else make_frames(timeline)
    try:
        return sr.stream_replay(
            iter(frames), url=url, token=token, source="mic",
            meeting_id="replay-test", language="ru", speed=speed,
            validate_endpoint=sr.LOOPBACK_VALIDATE_ENDPOINT, tail_s=0.5,
            feed_hook=feed_hook)
    finally:
        server.should_exit = True


def test_replay_healthy_streaming_semantics(monkeypatch):
    report = _run_replay(monkeypatch, [("s", 0.3), (340.0, 1.4), ("s", 1.2)],
                         mock_delay=0.08, ack_grace=6, hangover=600, speed=40)
    cl = report["client"]
    # full-total counters (not the capped sample)
    assert cl["outputs"]["finals_total"] >= 1
    assert cl["outputs"]["finals_total"] >= len(cl["outputs"]["observed_finals"])
    assert all(c["failed"] is None and c["ack_rel"] is not None
               for c in cl["commits"])
    counts = report["counts"]
    assert counts["uncovered_span_count"] == 0
    assert counts["client_gap_s"] == 0.0
    assert report["first_lost_stages"] == []
    assert report["original_pacing"] is False
    flat = json.dumps(report)
    assert "Финальн" not in flat and "Тестовый" not in flat
    assert "replay-token" not in flat


def test_replay_failure_negative_ack_timeout_restarts_without_replay(
        monkeypatch):
    report = _run_replay(monkeypatch, [("s", 0.3), (340.0, 1.4), ("s", 1.0)],
                         mock_delay=2.0, ack_grace=0.4, hangover=600, speed=1.0)
    assert report["original_pacing"] is True
    codes = [c["code"] for c in report["client"]["statuses"]]
    assert "commit_ack_timeout" in codes
    counts = report["counts"]
    assert counts["client_gap_s"] > 0.5
    assert counts["uncovered_span_count"] >= 1
    stages = report["first_lost_stages"]
    first = stages[0]
    assert first["stage"] == "client_stage"
    assert first["reason"] == "lost_in_restart_without_replay"
    assert first["client_code"] == "commit_ack_timeout"
    a, b = first["span_s"]
    assert abs(a - 0.3) < 0.6 and abs(b - 1.7) < 0.6
    # epoch-1 mapping: either the observed origin was absence (nothing had
    # been dequeued yet -> UNKNOWN correlation) or it is origin-relative
    sa = first.get("server_samples")
    if sa is None:
        # client-restart attribution never claims a sample mapping when no
        # origin was observed (unknown > fabricated evidence)
        assert first["stage"] == "client_stage"
    else:
        assert abs(sa[1] - int(b * 16000)) < 20000
    flat = json.dumps(report)
    assert "Финальн" not in flat and "replay-token" not in flat


def test_replay_epoch_correct_two_epoch_losses_and_mapping(monkeypatch):
    """Generation 1 loses its speech to an ACK timeout; generation 2 starts
    later and its speech is covered by finals; a SECOND, unrelated restart
    then occurs. Attribution must stay epoch membership: gen1 loss maps to
    its OWN restart + its own origin; the later restart never re-attributes
    gen1 and never fabricates losses for covered gen2 speech."""
    report = _run_replay(
        monkeypatch,
        [("s", 0.3), (340.0, 1.4), ("s", 1.0),
         (340.0, 1.4), ("s", 1.2)],
        mock_delay=2.0, ack_grace=1.2, hangover=600, speed=1.0)
    codes = [c["code"] for c in report["client"]["statuses"]]
    assert codes.count("commit_ack_timeout") >= 2      # both generations hit it
    counts = report["counts"]
    assert counts["client_gap_s"] > 1.0
    assert counts["uncovered_span_count"] >= 1
    assert report["client_epochs"] >= 2
    stages = report["first_lost_stages"]
    assert stages, stages
    assert len(stages) <= 5                            # only real missed spans
    for st in stages:
        # no later-restart inheritance into other epochs, no formalized
        # 'server_stage' claim: only epoch-tagged client attributions or
        # explicit candidates
        assert st["stage"] in ("client_stage", "server_stage_candidate",
                               "uncertain")
        if st["stage"] == "client_stage":
            assert st["reason"] in ("lost_in_restart_without_replay",
                                    "dropped_before_send", "queued_not_sent",
                                    "not_enqueued")
    gen1 = [s for s in stages if s["epoch"] == 1]
    assert gen1 and gen1[0]["client_code"] == "commit_ack_timeout"
    a, b = gen1[0]["span_s"]
    assert abs(a - 0.3) < 0.6 and abs(b - 1.7) < 0.6
    sa, sb = gen1[0]["server_samples"]
    assert 0 <= sa < 8000 and abs(sb - int(b * 16000)) < 6000  # epoch's origin
    gen2 = [s for s in stages if s["epoch"] == 2]
    for st in gen2:
        assert st["span_s"][0] >= 2.5                  # reconnect-epoch speech
        if "server_samples" in st and st["server_samples"] is not None:
            assert st["server_samples"][0] < 20200     # NOT absolute audio time


def test_replay_cumulative_samples_across_two_commits(monkeypatch):
    """(a) Two commits on ONE connection: the per-connection cumulative send
    counter must NOT reset at the successful ACK of commit #1; a loss after
    commit #2 maps to cumulative server abs samples (speech1+gap+speech2),
    matching the server's own `epoch_samples` accounting."""
    flipped = [False]

    def hook(idx, frames=make_frames([("s", 0.3), (340.0, 1.4), ("s", 1.0),
                                      (340.0, 1.4), ("s", 1.2)])):
        # once commit #1 has ACKed (grace was generous) and speech2 is feeding,
        # shorten the grace so ONLY commit #2 times out
        if not flipped[0] and frames[idx][0] >= 2.7:
            os.environ["LOQUI_REMOTE_COMMIT_ACK_GRACE_SECONDS"] = "0.35"
            flipped[0] = True
    report = _run_replay(
        monkeypatch,
        [("s", 0.3), (340.0, 1.4), ("s", 1.0), (340.0, 1.4), ("s", 1.2)],
        mock_delay=2.0, ack_grace=12, hangover=600, speed=1.0, feed_hook=hook)
    cl = report["client"]
    commits = cl["commits"]
    assert len(commits) >= 2
    assert commits[0]["failed"] is None and commits[0]["ack_rel"] is not None
    assert any(c["failed"] for c in commits[1:])
    codes = [c["code"] for c in cl["statuses"]]
    assert "commit_ack_timeout" in codes
    counted = [s for s in report["first_lost_stages"]
               if s["reason"] == "lost_in_restart_without_replay"
               and s["span_s"][0] > 2.0]
    assert counted, report["first_lost_stages"]
    st = counted[0]
    assert st["connection"] == 1 and st["epoch"] == 1  # same connection
    sa, sb = st["server_samples"]
    # cumulative across speech1+gap+silence fed then speech2: abs range
    assert 36800 < sa < 49600, (sa, sb)
    assert 57600 < sb < 73600, (sa, sb)


def test_replay_queue_membership_follows_actual_send_connection(monkeypatch):
    """(b)+(c) Speech queued while commit #1 fences, then kill (ACK timeout):
    the queued speech is NEVER sent (queue discarded) and must NOT carry any
    server sample interval nor a later connection; the speech queued AFTER
    reconnect is sent by connection #2 and maps with conn-2-cumulative
    (small) samples, not feed-time guesses."""
    report = _run_replay(
        monkeypatch,
        [("s", 0.2), (340.0, 2.0), ("s", 0.8),
         (340.0, 1.4), ("s", 1.0)],
        mock_delay=2.0, ack_grace=0.4, hangover=600, speed=1.0)
    stages = report["first_lost_stages"]
    assert report["client_epochs"] >= 2
    assert report["counts"]["uncovered_span_count"] >= 2
    for st in stages:
        if st["reason"] in ("queued_not_sent", "dropped_before_send"):
            assert st["server_samples"] is None and st["connection"] is None
            assert st["epoch"] == 0 or st["epoch"] is None or "epoch" not in st
        elif st["reason"] == "lost_in_restart_without_replay":
            assert st["stage"] == "client_stage"
            assert isinstance(st["connection"], int) and st["connection"] >= 1
            assert st["server_samples"] and st["server_samples"] >= [0, 0]
    gen1 = [s for s in stages if s["reason"] == "lost_in_restart_without_replay"
            and s["span_s"][0] < 2.5]
    assert gen1 and gen1[0]["connection"] == 1
    sa, sb = gen1[0]["server_samples"]
    assert 1000 < sa < 7000 and 28000 < sb < 42000   # cum on conn1 (~0.2-2.2s)
    gen2 = [s for s in stages if s.get("connection") == 2]
    assert gen2, stages                              # queued-after-reconnect
    sa2, sb2 = gen2[0]["server_samples"]
    assert sa2 < 16000 and sb2 < 40000               # conn-2-cumulative, NOT abs
    assert "Финальн" not in json.dumps(report)


def _identity_rows(report, ts_lo, ts_hi, eps=1e-6):
    # float-accumulated frame timestamps drift ~1e-9; widen only the lower
    # bound (upper bound stays strict so the next window never bleeds in)
    return [r for r in report["send_identities"]
            if ts_lo - eps <= r["ts"] < ts_hi]


def test_replay_exact_identity_a_b_c_equal_frames(monkeypatch):
    """A/B/C disjoint capture windows with EQUAL 640-byte frames through the
    ACTUAL frozen pipeline: identity (not length) decides mapping. A must map
    delivered on its connection with an exact contiguous cum chain; B must be
    discarded-with-NO range (never fabricated as later connection's send); the
    late C window maps to the SECOND connection starting at 0. All records fit
    the bounded report sample (200) so the identities are EXACTLY observable."""
    report = _run_replay(
        monkeypatch,
        [("s", 0.2), (340.0, 1.0), ("s", 0.6), (340.0, 1.4), ("s", 0.8)],
        mock_delay=2.0, ack_grace=0.3, hangover=600, speed=1.0)
    assert len(report["send_identities"]) <= sr.REPORT_LIMIT
    A = _identity_rows(report, 0.2, 1.2)   # sent before commit #1
    B = _identity_rows(report, 1.8, 2.1)   # queued, then discarded on restart
    C = [r for r in report["send_identities"] if r["conn"] == 2]
    assert A and B and C
    # A: EXACT contiguous delivered chain on connection 1, cumulative
    # [3200, 19200] (leading 0-0.2 s silence delivered too)
    assert all(r["state"] == "delivered" and r["conn"] == 1 for r in A)
    assert all(A[i]["s1d"] == A[i + 1]["s0d"] for i in range(len(A) - 1))
    assert A[0]["s0d"] == 3200 and A[-1]["s1d"] == 19200
    # B: really discarded across reconnect -> NO server range, NOT conn 2
    assert all(r["state"] == "discarded" and r["conn"] is None
               and r["s0d"] is None and r["s1d"] is None and r["s0a"] is None
               for r in B)
    # C: exact identity on connection 2, cum chain starting at 0
    assert all(r["state"] == "delivered" for r in C)
    assert all(C[i]["s1d"] == C[i + 1]["s0d"] for i in range(len(C) - 1))
    assert C[0]["s0d"] == 0
    # the stage rows bind to the identity windows exactly (no length guesses)
    st = {tuple(s["span_s"]): s for s in report["first_lost_stages"]}
    a_stage = st[(0.2, 1.2)]
    assert a_stage["connection"] == 1
    assert a_stage["server_samples"] == [3200, 19200]
    c_stage = next(s for s in report["first_lost_stages"]
                   if s.get("connection") == 2)
    c_rows = [r for r in report["send_identities"] if r["conn"] == 2
              and r["ts"] < c_stage["span_s"][1]]
    assert c_stage["server_samples"] == [c_rows[0]["s0d"], c_rows[-1]["s1d"]]
    assert all(s["server_samples"] is None
               for s in report["first_lost_stages"]
               if s["reason"] in ("queued_not_sent", "not_enqueued",
                                  "dropped_before_send"))


class ScriptedSocket:
    """Test-only inner socket for the connector swap point: every binary send
    consumes a scripted directive ("ok" | "raise" | "cancel"); text/control
    sends are served locally (session.updated / committed). No audio kept."""

    def __init__(self, script, ledger, t0):
        self.script = list(script)
        self.ledger = ledger
        self.t0 = t0
        self.started = time.monotonic()
        self.text_commits = 0
        self.inbound = deque([json.dumps({
            "type": "session.updated", "event_id": "s-identity",
            "session": {},
            "applied": {"language": "ru", "words": False}})])

    async def send(self, data):
        if isinstance(data, (bytes, bytearray)):
            directive = self.script.pop(0) if self.script else "ok"
            if directive == "raise":
                raise ConnectionError("scripted send failure")
            if directive == "cancel":
                raise asyncio.CancelledError("scripted cancelled send")
            return
        text = data.decode() if isinstance(data, bytes) else data
        if '"input_audio_buffer.commit"' in text:
            self.text_commits += 1
            self.inbound.append(json.dumps(
                {"type": "input_audio_buffer.committed", "event_id":
                 f"ack-{self.text_commits}", "audio_processed": 0.0}))
        self.inbound.appendleft(self.inbound.pop())  # keep ordering simple

    async def recv(self):
        while not self.inbound:
            await asyncio.sleep(0.005)
        return self.inbound.popleft()

    async def close(self):
        return None

    def __getattr__(self, name):
        raise AttributeError(name)


def _scripted_run(timeline, script, count, monkeypatch=None):
    frames = make_frames(timeline)
    captures = {}      # per connection: the ScriptedSocket instance
    send_ledger_holder = {}

    def connector(cfg, send_ledger):
        send_ledger.new_connection()
        tag = send_ledger.conn_count
        sock = ScriptedSocket(script if tag == 1 else ["ok"] * count,
                              send_ledger, send_ledger.t0)
        captures[tag] = sock

        async def connect():
            return sr._TracingSocket(sock, tag, send_ledger, send_ledger.t0)
        return connect()

    import os
    os.environ["LOQUI_REMOTE_COMMIT_HANGOVER_MS"] = "600"
    os.environ["LOQUI_REMOTE_COMMIT_ACK_GRACE_SECONDS"] = "12"
    return sr.stream_replay(
        iter(frames), url="wss://scripted.invalid/v1/audio/transcriptions/realtime",
        token="scripted-token", source="mic", meeting_id="replay-test",
        language="ru", speed=1.0,
        validate_endpoint=sr.LOOPBACK_VALIDATE_ENDPOINT,
        tail_s=0.5, connector=connector)


def test_replay_send_attempts_distinct_from_completions():
    """(2) Success, raised send, and cancelled-in-flight send through the
    ACTUAL frozen client (only the transport inner socket is scripted):
    no false success, uncertainty explicit, attempt counter keeps growing so
    later offsets on that connection are not silently assumed delivered."""
    # conn1: 5 ok sends then the FIRST SPEECH frame raised; conn2 (fresh
    # after the restart): delivered silence afterwards
    report = _scripted_run([("s", 0.1), (340.0, 0.5), ("s", 0.9)],
                           ["ok"] * 5 + ["raise"], count=6)
    rows = report["send_identities"]
    ok_first = [r for r in rows if r["state"] == "delivered" and r["conn"] == 1]
    assert len(ok_first) == 5                       # exactly five successes
    assert ok_first[0]["s0d"] == 0 and ok_first[-1]["s1d"] == 1600
    raised = [r for r in rows if r["state"] == "uncertain"]
    assert len(raised) == 1
    r0 = raised[0]
    assert r0["conn"] == 1 and r0["uncertain"] == "send_raised_or_cancelled"
    assert r0["s0a"] == 1600 and r0["s1a"] == 1920  # attempt-bounded
    assert r0["s0d"] is None and r0["s1d"] is None  # NO false success
    raise_stage = next(s for s in report["first_lost_stages"]
                       if s["stage"] == "uncertain")
    assert raise_stage["connection"] == 1
    assert raise_stage["server_samples_unconfirmed"] == [1600, 1920]
    # conn2 exists and restarts its own counters fresh (delivered chain 0..)
    conn2 = [r for r in report["send_identities"] if r["conn"] == 2]
    assert conn2 and conn2[0]["s0d"] == 0
    assert all(r["state"] != "delivered" or r["s1d"] > r["s0d"] >= 0
               for r in conn2)
    # cancellation variant: uncertainty recorded identically, no success
    report = _scripted_run([("s", 0.1), (340.0, 0.5), ("s", 0.9)],
                           ["ok"] * 5 + ["cancel"], count=6)
    cancelled = [r for r in report["send_identities"]
                 if r["state"] == "uncertain"]
    assert len(cancelled) == 1 and cancelled[0]["conn"] == 1
    assert cancelled[0]["s0d"] is None
    assert report["counts"]["uncertain_delivery_frames"] >= 1
    unc = [s for s in report["first_lost_stages"] if s["stage"] == "uncertain"]
    assert unc and unc[0]["reason"] == "delivery_uncertain"
    assert unc[0]["server_samples_unconfirmed"] == [1600, 1920]


def test_replay_send_attempts_second_attempt_after_failure_delivered():
    """After a raised send the connection resets its DELIVERED counter on the
    next attempt batch: no silent zero- or forward-delivery assumption."""
    report = _scripted_run([("s", 0.1), (340.0, 0.3), ("s", 0.6),
                            (340.0, 0.4), ("s", 0.6)],
                           ["ok", "raise"], count=8)
    rows = report["send_identities"]
    delivered_c1 = [r for r in rows if r["state"] == "delivered"
                    and r["conn"] == 1]
    delivered_c2 = [r for r in rows if r["state"] == "delivered"
                    and r["conn"] == 2]
    if delivered_c1:
        assert delivered_c1[0]["s0d"] == 0
    # raised item stays uncertain; nothing between delivered-on-c1 and
    # delivered-on-c2 claims continuous delivery
    uncertain = [r for r in rows if r["state"] == "uncertain"]
    assert uncertain and uncertain[0]["s0d"] is None
    for r in delivered_c2:
        assert r["s0d"] >= 0   # fresh chain, not continuation of c1


def test_replay_later_restart_never_fabricates_losses(monkeypatch):
    """All speech covered by finals; then an UNRELATED capture-gap restart on
    post-coverage silent frames. The restart must never retro-create losses
    from covered speech and must not be reported as a loss stage."""
    frames = make_frames([("s", 0.3), (340.0, 1.2), ("s", 0.9),
                          (340.0, 1.2), ("s", 0.9)])
    pos = len(frames) - 10                    # post-coverage silence only
    edited = frames[:pos] + [(frames[pos][0] + 6.0, frames[pos][1])] \
        + frames[pos + 1:]
    report = _run_replay(monkeypatch, None, mock_delay=0.08, ack_grace=6,
                         hangover=600, speed=1.0, frames=edited)
    codes = [c["code"] for c in report["client"]["statuses"]]
    assert "capture_gap" in codes                       # the unrelated restart
    counts = report["counts"]
    assert counts["uncovered_span_count"] == 0          # no fabricated losses
    assert report["first_lost_stages"] == []


# ---------------- complete coverage beyond capped samples -------------------

def test_ledger_coverage_complete_beyond_report_sample():
    led = sr.ReplayLedger(source="mic", capture_t0=time.monotonic())
    for i in range(300):
        s = 10.0 + i * 0.5
        seg = type("S", (), {"status": "final", "t_start": s, "t_end": s + 0.3,
                             "words": [], "meeting_id": "m", "source": "mic",
                             "text": "t", "seg_id": "x"})
        led.on_emit(seg)
    assert led.final_spans_total == 300                 # accurate total
    assert len(led.emits) == 200                        # capped sample only
    assert len(led.covered) == 300                      # COMPLETE accounting
    assert math.isclose(sum(e - s for s, e in led.covered), 90.0)
    assert math.isclose(led.covered[0][0], 10.0)
    # speech overlapping only UNCOVERED tails stays uncovered; covered holes
    # subtract exactly
    speech = [(10.0, 160.0)]
    uncovered = sr._subtract(speech, led.covered)
    assert len(uncovered) == 300                        # the 0.2 s gaps
    for s, e in uncovered:
        assert math.isclose(e - s, 0.2)


def test_replay_speed_validation():
    for bad in (0, -1, float("inf"), float("nan"), "x", True, 1000.0):
        with pytest.raises(ValueError):
            sr.stream_replay(iter([]), url="ws://x", token="t", speed=bad)


# ---------------- thin CLI: WAV parsing + token handling ---------------------

def _wav_bytes(rate_bad=None, seconds=0.5):
    import wave
    buf = _io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate_bad or 16000)
        w.writeframes(speech_pcm(seconds))
    return buf.getvalue()


def test_cli_wav_parses_and_preserves_final_short_frame():
    import wave as wf
    buf = _io.BytesIO()
    with wf.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        # 0.5 s + 100 extra samples: NOT a multiple of the 320-sample frame
        w.writeframes(speech_pcm(0.5) + speech_pcm(100.0 / 16000))
    frames = sr.read_frames_stdin(_io.BytesIO(buf.getvalue()))
    assert len(frames) == 26                            # 25 full + short tail
    assert all(len(p) == 640 for _, p in frames[:25])
    tail_t, tail_pcm = frames[-1]
    assert len(tail_pcm) == 200                         # 100 samples preserved
    assert abs(tail_t - 0.5) < 1e-6                     # audio position kept


def test_cli_rejects_wrong_rate_and_bad_speed_and_missing_token(
        tmp_path, monkeypatch):
    with pytest.raises(SystemExit):
        sr.read_frames_stdin(_io.BytesIO(_wav_bytes(rate_bad=44100)))
    monkeypatch.delenv("FSCRIBE_TOKEN", raising=False)
    tokfile = tmp_path / "tok.env"
    tokfile.write_text("FS_DUMMY=d\nFSCRIBE_TOKEN=abc123\n", encoding="utf-8")
    assert sr.load_token_file(str(tokfile)) == "abc123"
    with pytest.raises(SystemExit):
        sr.load_token_file(str(tmp_path / "missing.env"))
    with pytest.raises(SystemExit):
        sr.main(["--url", "ws://x", "--speed", "0"])
    with pytest.raises(SystemExit):
        sr.main(["--url", "ws://x", "--speed", "inf"])
