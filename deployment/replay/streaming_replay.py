#!/usr/bin/env python
"""Reusable streaming replay through the ACTUAL frozen Loqui RemotePipeline.

TWO DISTINCT SEMANTICS (never conflate; owner refinement 2026-10-17):

1. FILE RETRANSCRIPTION (best transcript, potentially batch) is Loqui's
   EXISTING local-Whisper action (apps/desktop/src/main/postprocess/pipeline.ts,
   requestLocalRetranscription). It is NOT built here and its success NEVER
   counts as streaming acceptance.

2. STREAMING REPLAY (this module) feeds captured audio through the actual
   frozen RemotePipeline (snapshot only for import; ONLY the transport
   connector is replaced) at ORIGINAL pacing, so the real client queue /
   send-blocking / commit fence / ACK / restart-without-replay lifecycle runs
   exactly as in a live meeting.

PRIVACY (hard): CLI audio arrives via stdin ONLY (no private path in argv);
reports contain NO text, NO words, NO audio bytes, NO token. First-lost-stage
reporting is identity-exact: the pipeline's ACTUAL queue is observed in place
(qid per audio item) and joined to real transport send attempts
(`_TracingSocket`), so send membership = the connection that really
delivered; raised/cancelled sends are explicitly UNCERTAIN (attempt-bounded,
never claimed sent, never assumed absent); per-connection cumulative sample
counters (delivered-only for exact mapping) survive successful commits and
reset only on a NEW connection — matching the server's own `epoch_samples`.
Evidence is never fabricated: a span that was never actually transmitted
carries no server range and no connection claim; a span delivered by the
actual client but not covered by any accepted final is reported as a CANDIDATE
(server_stage candidate needs server-side lifecycle-trace correlation to be
confirmed). Temporal final-span coverage is diagnostic only — it is not proof
that every word was preserved end-to-end.

Sanitized output only. Repository placement: deployment/replay/, not installed
into any package; no Mac edits, no new dependencies.
"""
import argparse
import bisect
import collections
import io
import json
import math
import os
import queue as _q
import sys
import time
import wave
from collections import deque

# Frozen dated snapshot (metadata-only import). Overridable for a future
# updated snapshot; no Mac source edits in this phase.
SIDECAR = os.environ.get(
    "FSCRIBE_REPLAY_SIDECAR",
    "/tmp/4scribe-loqui-reference-2026-10-16/sidecar")
if SIDECAR not in sys.path:
    sys.path.insert(0, SIDECAR)

from loqui_sidecar.audio_ingest import DecodedFrame  # noqa: E402
from loqui_sidecar.transcription.remote import (  # noqa: E402
    RemoteConfig, RemotePipeline, _apply_input_gain, _env_seconds,
    _frame_speech_seconds)

#: endpoint used ONLY to satisfy RemoteConfig.validate() when replaying to a
#: loopback target (the connector is the actual transport; same pattern as
#: deployment/diagnostics/diagnose_real_pipeline.py). Production replay passes
#: the real tailnet URL as BOTH connector target and validate endpoint.
LOOPBACK_VALIDATE_ENDPOINT = "wss://replay.invalid/v1/audio/transcriptions/realtime"

REPORT_LIMIT = 200       # bounded LIST payload in the report (samples only)
FRAME_S = 0.02
SAMPLE_RATE = 16000
MAX_SPEED = 50.0         # test acceleration ceiling; 1.0 == original pacing

CLIENT_DROPPING_CODES = {
    "capture_gap", "queue_exhausted", "connection_lost", "audio_gap",
    "commit_ack_timeout", "generation_unfinalized_speech",
}


class SendLedger:
    """EXACT queue/transport observation: identity-based, length-free.

    Identity comes from the REAL PipelineQueue (the actual queue object the
    frozen client uses, observed in-place): each 'audio' item gets a monotonic
    qid. Dequeue happens either by the sender (then a TRANSPORT send attempt
    binds that qid to its connection) or by `_discard_queue` (drain under the
    pipeline lock: the observed drain mode around the per-instance discard
    call makes that distinction exact). The transparent socket wrapper records
    ATTEMPT and COMPLETION separately: delivered only after the awaited inner
    send returns; raised/cancelled sends are explicitly UNCERTAIN delivery
    (never claimed sent, never assumed absent at the server).

    Cumulative sample counters are per CONNECTION: delivered (successful
    sends only) and attempted (every attempt, kept to bound uncertainty for
    subsequent frames on the same connection). Successful commits NEVER reset
    them; a new connection starts fresh at 0 — matching the server's
    per-connection `epoch_samples`.

    NO audio/text is retained anywhere: only qid ids, sample COUNTS, capture
    timestamps and connection/state numbers."""

    def __init__(self, t0: float):
        self.t0 = t0
        self.conn_count = 0
        self.records = []            # per qid: metadata dict (see below)
        self._audio_qids = deque()   # FIFO over enqueued audio items
        self._pending_send = deque()  # qids really dequeued by the sender
        self.delivered_cum = {}
        self.attempt_cum = {}
        self.conn_uncertain = {}
        self.draining = False        # toggled around _discard_queue (lock-safe)

    def new_connection(self) -> int:
        self.conn_count += 1
        return self.conn_count

    # ---- real queue boundary ----

    def on_enqueued_audio(self, capture_ts: float, samples: int):
        qid = len(self.records) + 1
        self.records.append({"qid": qid, "ts": capture_ts, "samples": samples,
                             "state": "enqueued", "conn": None,
                             "s0d": None, "s1d": None, "s0a": None,
                             "s1a": None, "uncertain": None})
        self._audio_qids.append(qid)
        return qid

    def on_dequeued_audio(self, was_drain: bool):
        if not self._audio_qids:
            return
        qid = self._audio_qids.popleft()
        rec = self.records[qid - 1]
        if was_drain:
            rec["state"] = "discarded"
        else:
            rec["state"] = "dequeued"
            self._pending_send.append(qid)

    def on_attempt(self, conn: int, samples: int):
        """Transport attempt for the oldest dequeued-but-not-yet-bound item."""
        if not self._pending_send:
            return None
        qid = self._pending_send.popleft()
        rec = self.records[qid - 1]
        a1 = self.attempt_cum.get(conn, 0) + samples
        self.attempt_cum[conn] = a1
        rec["conn"] = conn
        rec["s0a"] = a1 - samples
        rec["s1a"] = a1
        return qid, rec

    def on_delivered(self, qid, rec):
        dsamples = rec["s1a"] - rec["s0a"]
        d1 = self.delivered_cum.get(rec["conn"], 0) + dsamples
        self.delivered_cum[rec["conn"]] = d1
        rec["s0d"], rec["s1d"] = d1 - dsamples, d1
        rec["state"] = "delivered"

    def on_delivery_uncertain(self, rec):
        rec["state"] = "uncertain"
        rec["uncertain"] = "send_raised_or_cancelled"
        self.conn_uncertain[rec["conn"]] = True

    def summary(self):
        return {"connections": self.conn_count,
                "sent_samples_delivered": {str(k): v
                                           for k, v in self.delivered_cum.items()},
                "sent_samples_attempted": {str(k): v
                                           for k, v in self.attempt_cum.items()}}


class _ObservedQueue(_q.Queue):
    """The ACTUAL pipeline queue observed in place (same class-based behavior,
    same maxsize, same underlying deque); counters only — no audio/text."""

    def __init__(self, maximum: int, ledger: "SendLedger"):
        super().__init__(maxsize=maximum)
        self._ledger = ledger

    def put_nowait(self, item):
        super().put_nowait(item)  # Full propagates to the frozen client path
        if isinstance(item, tuple) and item and item[0] == "audio":
            self._ledger.on_enqueued_audio(item[2], len(item[1]) // 2)

    def get_nowait(self):
        item = super().get_nowait()
        if isinstance(item, tuple) and item and item[0] == "audio":
            self._ledger.on_dequeued_audio(self._ledger.draining)
        return item


class _TracingSocket:
    """Transparent wrapper around the websockets Connection: observes binary
    send ATTEMPTS, then distinguishes completion (delivered) from raised or
    cancelled sends (delivery UNCERTAIN). Everything else forwards untouched."""

    def __init__(self, inner, conn_tag: int, ledger: "SendLedger", t0: float):
        self._inner = inner
        self._conn_tag = conn_tag
        self._ledger = ledger
        self._t0 = t0

    async def send(self, data):
        bound = None
        if isinstance(data, (bytes, bytearray)):
            if self._ledger._pending_send:
                qid = self._ledger._pending_send.popleft()
                samples = len(data) // 2
                rec = self._ledger.records[qid - 1]
                a1 = self._ledger.attempt_cum.get(self._conn_tag, 0) + samples
                self._ledger.attempt_cum[self._conn_tag] = a1
                rec["conn"] = self._conn_tag
                rec["s0a"], rec["s1a"] = a1 - samples, a1
                bound = (qid, rec)
            # empty pending: transport evidence without queue identity;
            # recorded below as uncertain-link only if no pending expected
        try:
            out = await self._inner.send(data)
        except BaseException:  # noqa: BLE001 - delivery is unknown here
            if bound is not None:
                self._ledger.on_delivery_uncertain(bound[1])
            raise
        if bound is not None:
            self._ledger.on_delivered(bound[0], bound[1])
        return out

    async def recv(self):
        return await self._inner.recv()

    async def close(self):
        return await self._inner.close()

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _merge(intervals, eps=0.06):
    """Merge overlapping/adjacent (start, end) intervals."""
    out = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1] + eps:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _subtract(base, cut):
    """base minus cut interval set (both merged)."""
    res = list(base)
    for cs, ce in cut:
        nxt = []
        for s, e in res:
            if ce <= s or cs >= e:
                nxt.append((s, e))
                continue
            if s < cs:
                nxt.append((s, cs))
            if e > ce:
                nxt.append((ce, e))
        res = nxt
    return [r for r in res if r[1] - r[0] > 1e-6]


class ReplayLedger:
    """Sanitized actual-client observations.

    Interval accounting is COMPLETE (final coverage is maintained as a merged
    interval set, never a capped sample); only the per-event report samples
    are capped at REPORT_LIMIT. Counters are always accurate totals."""

    def __init__(self, source: str, capture_t0: float):
        self.source = source
        self.t0 = capture_t0
        self.statuses = []
        self.emits = []              # bounded sample of outputs
        self.emits_total = 0         # accurate total
        self.final_spans_total = 0   # accurate total
        self.covered = []            # COMPLETE merged final coverage
        self.commits = []
        self.current_code = ""
        self.ready_count = 0         # client connection/epoch count

    def on_status(self, mid, src, state, reason="", code="", gap=0.0):
        if state == "ready":
            self.ready_count += 1
        self.current_code = code or ""
        self.statuses.append({
            "t_rel": round(time.monotonic() - self.t0, 3),
            "state": str(state)[:32],
            "reason": str(reason)[:120],     # frozen client message, not content
            "code": str(code)[:40],
            "gap_total": round(gap or 0.0, 3),
        })

    def on_emit(self, seg):
        self.emits_total += 1
        if getattr(seg, "status", "?") == "final":
            self.final_spans_total += 1
            self.add_final_span(getattr(seg, "t_start", 0.0),
                                getattr(seg, "t_end", 0.0))
        if len(self.emits) < REPORT_LIMIT:
            self.emits.append({
                "t_rel": round(time.monotonic() - self.t0, 3),
                "status": getattr(seg, "status", "?"),
                "span": [round(getattr(seg, "t_start", 0.0), 3),
                         round(getattr(seg, "t_end", 0.0), 3)],
                "n_words": len(getattr(seg, "words", None) or []),
            })

    def add_final_span(self, start, end):
        if not (isinstance(start, (int, float)) and isinstance(end, (int, float))
                and math.isfinite(float(start)) and math.isfinite(float(end))):
            return
        self.covered = _merge(self.covered + [(float(start), float(end))])

    def wrap_commit(self, pipeline):
        orig = pipeline._commit

        async def commit_ledger(socket):
            admit_rel = round(time.monotonic() - self.t0, 3)
            failed = None
            try:
                await orig(socket)
            except Exception as exc:  # noqa: BLE001 - client restarts anyway
                failed = type(exc).__name__
                raise
            finally:
                self.commits.append({
                    "admit_rel": admit_rel,
                    "ack_rel": None if failed else
                    round(time.monotonic() - self.t0, 3),
                    "wait_s": None if failed else
                    round(time.monotonic() - self.t0 - admit_rel, 3),
                    "failed": failed,
                })
        pipeline._commit = commit_ledger  # per-instance wrap only

    def summary(self, cap=REPORT_LIMIT):
        """observed = capped SAMPLE for the report; all count fields below are
        FULL accurate totals (never derived from the capped sample)."""
        observed = [e for e in self.emits if e["status"] == "final"][-cap:]
        finals_total = self.final_spans_total
        return {
            "statuses": self.statuses[-cap:],
            "statuses_total": len(self.statuses),
            "outputs": {
                "finals_total": finals_total,
                "partials_total": self.emits_total - finals_total,
                "emits_total": self.emits_total,
                "observed_finals": observed,
                "observed_sample_n": len(observed),
            },
            "commits": self.commits[-cap:],
        }


def create_replay_pipeline(*, url, token, source="mic", meeting_id="replay",
                           language="ru", endpointing_ms=800,
                           validate_endpoint=None, connector=None):
    """The ACTUAL frozen RemotePipeline; ONLY the transport connector is
    replaced (default: looping connector + transparent send-observing socket
    wrapper; tests may inject a scripted connector). The pipeline's ACTUAL
    queue object is observed in place: identity-based qid linkage between
    enqueue/dequeue and the transport send attempt (no audio/text retained).
    Returns (pipeline, ledger)."""
    import websockets  # frozen client-side dependency, already installed

    config = RemoteConfig(
        endpoint=validate_endpoint or url,
        language=language, endpointing_ms=endpointing_ms, token=token)
    ledger = ReplayLedger(source=source, capture_t0=time.monotonic())
    send_ledger = SendLedger(ledger.t0)
    ledger.send_ledger = send_ledger
    if connector is None:

        def user_connector(cfg):
            conn_tag = send_ledger.new_connection()

            async def connect():
                inner = await websockets.connect(
                    url, open_timeout=10,
                    additional_headers={"authorization": "Bearer " + token})
                return _TracingSocket(inner, conn_tag, send_ledger, ledger.t0)
            return connect()
    else:
        # test-only scripted connectors receive the pipeline's ledger so their
        # connection tags/observations stay in the SAME identity ledger
        def user_connector(cfg):
            return connector(cfg, send_ledger)

    def on_emit(seg):
        ledger.on_emit(seg)

    def on_status(mid, src, state, reason="", code="", gap=0.0):
        ledger.on_status(mid, src, state, reason, code, gap)

    pipeline = RemotePipeline(meeting_id, source, on_emit, config,
                              status=on_status, connector=user_connector)
    # observe the REAL queue in place (identity seam); the drain-mode toggle
    # keeps sender dequeues distinct from `_discard_queue` drains (both
    # acquire the same pipeline lock, so the toggle cannot race a sender pop)
    send_ledger.queue_max = pipeline.queue.maxsize
    pipeline.queue = _ObservedQueue(pipeline.queue.maxsize, send_ledger)
    orig_discard = pipeline._discard_queue

    def discard_observing():
        send_ledger.draining = True
        try:
            orig_discard()
        finally:
            send_ledger.draining = False
    pipeline._discard_queue = discard_observing  # per-instance observation
    ledger.wrap_commit(pipeline)
    return pipeline, ledger


def _epoch_of(wall, ready_t):
    """Client epoch index = number of successful `ready` transitions at or
    before this wall time (each reconnect starts a new epoch)."""
    return bisect.bisect_right(ready_t, wall)


def _part_epochs(frames, span):
    """Split an uncovered span into maximal parts with the SAME actual-send
    connection (identity-bound via the queue/dequeue seam), or a shared 'no
    transport evidence' group. Uncertainty is tracked per part: a raised or
    cancelled send makes the part's sample offsets ambiguous."""
    a, b = span
    related = [f for f in frames if f["t1"] > a + 1e-9 and f["t0"] < b - 1e-9]

    def _key(f):
        return f.get("conn") if f.get("sent_or_uncertain") else None

    parts = []
    cur = None
    for f in related:
        key = _key(f)
        if cur is not None and cur["conn"] == key:
            cur["t1"] = max(cur["t1"], f["t1"])
            if f["dropping"]:
                cur["dropping"] = True
            if f.get("sent_or_uncertain"):
                cur["s0"] = min(cur["s0"], f["s0a"]) if cur["s0"] is not None else f["s0a"]
                cur["s1"] = max(cur["s1"], f["s1a"]) if cur["s1"] is not None else f["s1a"]
                if f.get("s0d") is not None:
                    cur["d0"] = min(cur["d0"], f["s0d"]) if cur["d0"] is not None else f["s0d"]
                    cur["d1"] = max(cur["d1"], f["s1d"]) if cur["d1"] is not None else f["s1d"]
                if f.get("uncertain"):
                    cur["uncertain"] = True
            cur["walls"].append(f["wall"])
            cur["frames_n"] += 1
        else:
            if cur is not None:
                parts.append(cur)
            cur = {"conn": key, "t0": max(a, f["t0"]), "t1": min(b, f["t1"]),
                   "dropping": f["dropping"],
                   "code": f["code"] if f["dropping"] else "",
                   "sent_or_uncertain": bool(f.get("sent_or_uncertain")),
                   "s0": f.get("s0a") if f.get("sent_or_uncertain") else None,
                   "s1": f.get("s1a") if f.get("sent_or_uncertain") else None,
                   "d0": f.get("s0d") if f.get("sent_or_uncertain") else None,
                   "d1": f.get("s1d") if f.get("sent_or_uncertain") else None,
                   "uncertain": bool(f.get("uncertain")),
                   "all_queued_notsent": f["qid"] is None and not
                   f.get("sent_or_uncertain"),
                   "walls": [f["wall"]], "frames_n": 1}
    if cur is not None:
        parts.append(cur)
    return parts


def _first_lost_stage(uncovered, frames, dropping_intervals, restart_windows,
                      ready_t, rate, base):
    """Trace the FIRST lost stage (not only gap totals), SEND-IDENTITY based:

    - send membership: a frame's connection = the connection that the ACTUAL
      queue dequeue bound to a real transport attempt (identity via the
      observed real queue, never length matching); enqueued-but-never-dequeued
      (discarded) and never-enqueued (fed while dropping) frames have NO
      connection and no sample interval.
    - per-connection cumulative accounting: delivered-only offsets for exact
      correlation; a raised/cancelled send marks the part's delivery UNCERTAIN
      (attempt cum bounds provided; never claimed sent and never assumed
      absent at the server).
    - restart membership: restart windows are epoch-tagged via successful
      `ready` transitions; only speech actually delivered/attempted on that
      connection is attributed to that connection's restart.
    - a delivered-but-never-covered span is a CANDIDATE, not a confirmed
      server-side loss: confirmation needs the service lifecycle trace; no
      cause is inferred solely from final-time coverage."""
    def wall_of_audio_end(t):
        return (base + t * rate) if rate else None

    stages = []
    for span in uncovered:
        for part in _part_epochs(frames, span):
            pa, pb = part["t0"], part["t1"]
            stage = {"span_s": [round(pa, 3), round(pb, 3)],
                     "epoch": part["conn"] if part["conn"] is not None else 0}
            matched_drop = next(
                ((s, e, code) for s, e, code in dropping_intervals
                 if s <= (pa + pb) / 2.0 <= e), None)
            if matched_drop is not None:
                stage.update(stage="client_stage", reason="dropped_before_send",
                             client_code=matched_drop[2], server_samples=None,
                             connection=None)
            elif part["conn"] is None:
                stage.update(
                    stage="client_stage",
                    reason=("not_enqueued" if part["all_queued_notsent"]
                            else "queued_not_sent"),
                    client_code="", server_samples=None, connection=None,
                    evidence="no transport send evidence; the client did not "
                             "transmit these frames")
            elif part["uncertain"] and part["s0"] is not None:
                stage.update(
                    stage="uncertain", reason="delivery_uncertain",
                    client_code="", connection=part["conn"],
                    server_samples_unconfirmed=[max(0, int(part["s0"])),
                                                max(0, int(part["s1"]))],
                    evidence=("a transport send on connection "
                              f"#{part['conn']} raised or was cancelled; "
                              "delivery to the server is unknown "
                              "(neither confirmed sent nor confirmed absent)"))
            else:
                conn = part["conn"]
                restarts = [(s, e, c) for s, e, c in restart_windows
                            if _epoch_of(s, ready_t) == conn]
                t_end_rel = max(part["walls"]) if part["walls"] else None
                anchor = next(
                    (r for r in restarts
                     if t_end_rel is not None
                     and (r[0] <= t_end_rel or
                          (wall_of_audio_end(pb) is not None
                           and r[0] >= wall_of_audio_end(pb) - 1e-3))),
                    None)
                samples = ([max(0, int(part["d0"])), max(0, int(part["d1"]))]
                           if part["d0"] is not None else None)
                if anchor is not None:
                    stage.update(stage="client_stage",
                                 reason="lost_in_restart_without_replay",
                                 client_code=anchor[2],
                                 server_samples=samples, connection=conn)
                else:
                    stage.update(stage="server_stage_candidate",
                                 reason="sent_uncovered_final",
                                 client_code="", connection=conn,
                                 server_samples=samples,
                                 evidence="candidate_not_confirmed; correlate "
                                          "server_samples (cumulative per "
                                          "connection, delivery-confirmed) "
                                          "with fscribe-lifecycle trace "
                                          "abs_*_samp of connection "
                                          f"#{conn}")
            stages.append(stage)
    return stages


def stream_replay(frames, *, url, token, source="mic", meeting_id="replay",
                  language="ru", endpointing_ms=800, speed=1.0,
                  validate_endpoint=None, tail_s=1.0, frame_s=FRAME_S,
                  feed_hook=None, connector=None):
    """Feed (timestamp_s, pcm) frames through the actual pipeline at original
    pacing (frame_s / speed; speed == 1.0 is ORIGINAL pacing; >1 is TEST-ONLY
    acceleration and is flagged in the report). Identity evidence comes from
    the observed REAL pipeline queue joined to the transport send attempts —
    no length/time guessing. No audio/text is retained anywhere.

    feed_hook: optional test-only callable(frame_index) invoked after each
    frame feed (used by focused regressions to time-mutate env knobs that the
    frozen client reads per commit; production replays pass nothing).
    connector: optional connector factory (test-only scripted sockets).
    Returns the sanitized report."""
    if not isinstance(speed, (int, float)) or isinstance(speed, bool) \
            or not math.isfinite(float(speed)) or not 0 < float(speed) <= MAX_SPEED:
        raise ValueError(f"speed must be finite and in (0, {MAX_SPEED}]; got "
                         f"{speed!r}")
    pipeline, ledger = create_replay_pipeline(
        url=url, token=token, source=source, meeting_id=meeting_id,
        language=language, endpointing_ms=endpointing_ms,
        validate_endpoint=validate_endpoint, connector=connector)
    send_ledger = ledger.send_ledger

    n_frames = 0
    dropped_frames = 0
    queue_high_water = 0
    dropping_intervals = []     # (audio_start, audio_end, code_at_feed)
    dropping_open = None
    speech_intervals = []       # client-classified per-frame speech
    frames_data = []            # per-frame identity-bound records (no audio)
    capture_t0 = ledger.t0
    t_end = 0.0
    gain = _env_seconds("LOQUI_REMOTE_INPUT_GAIN", 3.0)

    try:
        for t_s, pcm in frames:
            wall = time.monotonic() - capture_t0
            # mirror the client's own speech ledger basis: gain applied first
            # (mic), same _frame_speech_seconds classifier
            probe = _apply_input_gain(pcm, gain) if (source == "mic" and
                                                     gain != 1.0) else pcm
            speech = _frame_speech_seconds(probe) > 0
            if speech:
                speech_intervals.append((t_s, t_s + len(pcm) / 32000.0))
            with pipeline.lock:
                dropping = pipeline.dropping_audio
                code = ledger.current_code
            q_before = len(send_ledger.records)
            frames_data.append({"t0": t_s, "t1": t_s + len(pcm) / 32000.0,
                                "dropping": dropping, "code": code,
                                "qid": None, "wall": wall, "conn": None,
                                "sent_or_uncertain": False, "uncertain": False,
                                "s0a": None, "s1a": None,
                                "s0d": None, "s1d": None})
            pipeline.feed(DecodedFrame(source=source, seq=n_frames + 1,
                                       timestamp_ms=t_s * 1000.0, pcm=pcm))
            # EXACT capture identity: the observed real queue got 0 or 1 audio
            # item from this feed call; its qid binds the capture frame
            n_new = len(send_ledger.records) - q_before
            if n_new == 1:
                frames_data[-1]["qid"] = send_ledger.records[-1]["qid"]
            n_frames += 1
            if dropping:
                dropped_frames += 1
                if dropping_open is None:
                    dropping_open = t_s
            elif dropping_open is not None:
                dropping_intervals.append((dropping_open, t_s, code))
                dropping_open = None
            with pipeline.lock:
                queue_high_water = max(queue_high_water, pipeline.queued_bytes)
            t_end = max(t_end, t_s + len(pcm) / 32000.0)
            if feed_hook is not None:
                feed_hook(n_frames - 1)
            time.sleep(frame_s / speed)
        if dropping_open is not None:
            dropping_intervals.append((dropping_open, t_end, ledger.current_code))
        time.sleep(tail_s / speed)
    finally:
        pipeline.finish()

    # IDENTITY reconciliation from the observed real queue records: each
    # enqueued item was either dequeue-bound to a transport attempt (conn +
    # delivered-only cum range / uncertain attempt range) or drained by
    # `_discard_queue` (never sent). No length guessing anywhere.
    for f in frames_data:
        if f["qid"] is None:
            continue
        rec = send_ledger.records[f["qid"] - 1]
        state = rec["state"]
        if state in ("delivered", "uncertain"):
            f["conn"] = rec["conn"]
            f["sent_or_uncertain"] = True
            f["uncertain"] = state == "uncertain"
            f["s0a"], f["s1a"] = rec["s0a"], rec["s1a"]
            f["s0d"], f["s1d"] = rec["s0d"], rec["s1d"]
        # state in ("enqueued", "discarded") -> no transport evidence

    # span reconstruction survives generation restarts (the pipeline clears
    # its own span ledger per generation): captured client speech minus the
    # COMPLETE merged set of accepted final coverage
    speech = _merge(speech_intervals)
    uncovered = _subtract(speech, ledger.covered)

    restart_windows = []   # (start_rel, end_rel, triggering code)
    open_start = open_code = None
    for st in ledger.statuses:
        if st["code"] in CLIENT_DROPPING_CODES:
            if open_start is None:
                open_start = st["t_rel"]
                open_code = st["code"]
        elif open_start is not None:
            restart_windows.append((open_start, st["t_rel"], open_code))
            open_start = open_code = None
    if open_start is not None:
        restart_windows.append((open_start, ledger.statuses[-1]["t_rel"],
                                open_code))

    ready_t = [s["t_rel"] for s in ledger.statuses if s["state"] == "ready"]
    if frames_data and frames_data[-1]["t0"] > 0:
        rate = frames_data[-1]["wall"] / frames_data[-1]["t0"]
        base = frames_data[0]["wall"] - frames_data[0]["t0"] * rate
        base = max(0.0, base)
    else:
        rate, base = 0.0, 0.0

    stages = _first_lost_stage(uncovered, frames_data, dropping_intervals,
                               restart_windows, ready_t, rate, base)
    delivered = uncertain = enq_not_sent = not_enq = 0
    for f in frames_data:
        if f["conn"] is not None and not f["uncertain"]:
            delivered += 1
        elif f["sent_or_uncertain"]:
            uncertain += 1
        elif f["qid"] is not None:
            enq_not_sent += 1
        else:
            not_enq += 1
    return {
        "report": "streaming-replay",
        "version": 4,
        "url": url,
        "source": source,
        "meeting_id": meeting_id,
        "language": language,
        "original_pacing": speed == 1.0,
        "frame_s": frame_s,
        "speed": speed,
        "client_epochs": len(ready_t),
        "counts": {
            "frames_fed": n_frames,
            "dropped_frames_replayed": dropped_frames,
            "queued_not_sent_frames": enq_not_sent,
            "not_enqueued_frames": not_enq,
            "delivered_frames": delivered,
            "uncertain_delivery_frames": uncertain,
            "queue_high_water_bytes": queue_high_water,
            "audio_s": round(t_end, 3),
            "client_gap_s": round(pipeline.gap_seconds, 3) or 0.0,
            "last_final_end_s": round(pipeline.last_final_end, 3),
            "uncovered_speech_spans": [[round(s, 3), round(e, 3)]
                                       for s, e in uncovered[-REPORT_LIMIT:]],
            "uncovered_span_count": len(uncovered),
            "final_coverage_spans": len(ledger.covered),
            "final_coverage_total_s": round(
                sum(e - s for s, e in ledger.covered), 3),
        },
        # bounded IDENTITY ledger (metadata only: qid, capture ts, counts,
        # per-connection cumulative bounds, state; never audio/text)
        "send_identities": [
            {k: r[k] for k in ("qid", "ts", "samples", "state", "conn",
                               "s0d", "s1d", "s0a", "s1a", "uncertain")}
            for r in send_ledger.records[-REPORT_LIMIT:]],
        "first_lost_stages": stages[-REPORT_LIMIT:],
        "client": ledger.summary(),
    }


# ---------------- thin diagnostic CLI (stdin WAV, no private paths) ----------

def _fail(msg):
    print(msg, file=sys.stderr)
    raise SystemExit(2)


def read_frames_stdin(stream, frame_s=FRAME_S):
    """Parse a WAV (pcm_s16le, mono, 16 kHz - the recorded client format) from
    the given binary stream; no data is echoed, no resampling. A shorter
    final frame is PRESERVED (not truncated away) so the very end of a
    recording is never silently lost."""
    data = stream.read()
    try:
        with wave.open(io.BytesIO(data)) as w:
            nch, sw, fr = w.getnchannels(), w.getsampwidth(), w.getframerate()
            if (nch, sw, fr) != (1, 2, SAMPLE_RATE):
                _fail("expected PCM16 mono 16 kHz WAV; got "
                      f"channels={nch} width={sw} rate={fr} (no resampling)")
            raw = w.readframes(w.getnframes())
    except wave.Error:
        _fail("stdin is not a readable WAV file")
    frames = []
    per_frame = int(frame_s * SAMPLE_RATE) * 2
    idx = 0
    audio_pos = 0.0
    while idx < len(raw) - 1:
        chunk_len = min(per_frame, (len(raw) - idx) // 2 * 2)  # even bytes
        if chunk_len <= 0:
            break
        frames.append((round(audio_pos, 4), raw[idx:idx + chunk_len]))
        audio_pos += chunk_len / (2.0 * SAMPLE_RATE)
        idx += chunk_len
    return frames


def load_token_file(path):
    """Read the designated token file; keep value in RAM only. The file is
    never re-read, printed, summarized or copied."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("FSCRIBE_TOKEN="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        return val
    except OSError:
        _fail("token file unreadable (content never printed)")
    _fail("no FSCRIBE_TOKEN line found in the designated token file "
          "(content never printed)")


def _speed_arg(raw):
    try:
        v = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("speed must be a number")
    if not math.isfinite(v) or not 0 < v <= MAX_SPEED:
        raise argparse.ArgumentTypeError(
            f"speed must be finite in (0, {MAX_SPEED}]")
    return v


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Streaming replay of a WAV (stdin) through the actual "
                    "Loqui RemotePipeline to a 4scribe server; metadata-only "
                    "sanitized report on stdout.")
    ap.add_argument("--url", required=True, help="WS/WSS realtime endpoint "
                    "(loopback or wss tailnet path)")
    ap.add_argument("--token-file", help="token file with FSCRIBE_TOKEN= "
                    "(RAM only, never printed). Falls back to FSCRIBE_TOKEN "
                    "env if omitted.")
    ap.add_argument("--source", choices=["mic", "system"], default="mic")
    ap.add_argument("--language", choices=["ru", "en"], default="ru")
    ap.add_argument("--endpointing-ms", type=int, default=800)
    ap.add_argument("--meeting-id", default="replay")
    ap.add_argument("--speed", type=_speed_arg, default=1.0,
                    help="1.0 = original pacing (production); >1 is "
                         "TEST-ONLY acceleration, flagged in the report")
    ap.add_argument("--events", action="store_true",
                    help="print bounded client event summary too")
    args = ap.parse_args(argv)

    frames = read_frames_stdin(sys.stdin.buffer)
    token = load_token_file(args.token_file) if args.token_file \
        else os.environ.get("FSCRIBE_TOKEN") or ""
    if not token:
        _fail("no client credential: pass --token-file or set FSCRIBE_TOKEN "
              "env (value never printed)")
    # RemoteConfig.validate() requires a wss:// endpoint WITHOUT credentials;
    # loopback ws:// targets therefore validate against the neutral endpoint
    # (the connector is the actual transport; no token exists in the URL).
    validate_endpoint = LOOPBACK_VALIDATE_ENDPOINT \
        if args.url.startswith("ws://") else None
    report = stream_replay(iter(frames), url=args.url, token=token,
                           source=args.source, meeting_id=args.meeting_id,
                           language=args.language,
                           endpointing_ms=max(args.endpointing_ms, 800),
                           validate_endpoint=validate_endpoint,
                           speed=args.speed)
    report["client"].pop("statuses", None)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
