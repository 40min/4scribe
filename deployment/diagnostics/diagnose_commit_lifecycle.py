#!/usr/bin/env python
"""Bounded commit/queue/lifecycle diagnostic (owner-reported Loqui gate: first
and last phrase recognized, MIDDLE phrases omitted).

HERMETIC: the REAL 4scribe app runs against a MOCK Ollama (no real ASR loads,
no cloud calls, no private audio) driven by a faithful reference-client
re-implementation of the FROZEN Loqui pipeline semantics (remote.py from the
read-only snapshot):
- feed(): every 20 ms PCM frame ENQUEUED (silence included); byte budget 10 s;
  gap accounting = unfinalized SPEECH spans + discarded queued speech only;
- client VAD commits on speech-segment close after 2000 ms silence hangover
  (RemotePipeline VadConfig override) plus forced commit at
  stream_end - origin >= 30 s;
- sender fenced waiting the commit ACK (grace 12 s); restart WITHOUT replay on
  wire error / commit-ack timeout / queue exhaustion (queue discarded, stream
  continues into the fresh session - no replay of discarded speech).

Span-only instrumentation: NO transcripts/tokens/private data. The ledger
tracks each span through the stages:
  capture -> enqueueSpeech -> wireSend -> serverAdmit -> decode (or decline) ->
  emit(final/delta/ack/error) -> accept/stale-reject
and reports which stage FIRST lost coverage of a span, per scenario.

Scenarios (mock-only, short paced compositions - no campaigns):
  empty-decode              one phrase class decodes to EMPTY content with NO
                            wire error -> silent coverage skip at emission.
  final-backlog-overflow    quick phrases + one slow decode -> admission
                            eviction sends wire error -> client restart.
  forced-commit-slow-chain  30 s forced commit mid long phrase + slow decode
                            chain -> ACK fence pressure / grace crossing.
"""
import argparse
import asyncio
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import websockets  # noqa: E402
import uvicorn  # noqa: E402
from fourscribe import app as appmod  # noqa: E402
from fourscribe.config import Config  # noqa: E402
from tests.test_service_http import _free_port  # noqa: E402
from tests.mock_ollama import MockOllama  # noqa: E402

SAMPLE_RATE = 16000
FRAME = 640                       # 20 ms PCM16 mono bytes
QUEUE_SECONDS = 10.0
ACK_GRACE_S = 12.0
FORCE_COMMIT_S = 30.0
HANGOVER_S = 2.0                  # RemotePipeline hangover override (2000 ms)


def speech_pcm(seconds, freq=220.0, amp=9000.0):
    import array, math
    out = array.array("h")
    n = int(seconds * SAMPLE_RATE)
    for i in range(n):
        out.append(int(amp * math.sin(2 * math.pi * freq * i / SAMPLE_RATE)))
    return out.tobytes()


def silence_pcm(seconds):
    return b"\x00\x00" * int(seconds * SAMPLE_RATE)


class DiagOllama(MockOllama):
    """MockOllama + per-frequency decode wall delay + empty-content class."""

    def __init__(self, delay_by_freq=None, empty_for_freq=()):
        self.delay_by_freq = dict(delay_by_freq or {})
        self.empty_for_freq = list(empty_for_freq)
        super().__init__(delay_s=0.0)

    def _delay_for(self, pcm, rate):
        est = self._freq_text(pcm, rate)
        if est is None:
            return self.delay_by_freq.get("default", 0.0)
        for key in self.delay_by_freq:
            try:
                if abs(est - float(key)) <= 40.0:
                    return self.delay_by_freq[key]
            except (TypeError, ValueError):
                continue
        return self.delay_by_freq.get("default", 0.0)

    def chat(self, body):
        pcm, rate = self._payload_pcm(body)
        est = self._freq_text(pcm, rate)
        if est is not None and any(abs(est - k) <= 40.0 for k in self.empty_for_freq):
            return httpx.Response(200, json={"message": {"content": ""},
                                             "done": True, "eval_count": 1})
        delay = self._delay_for(pcm, rate)
        if delay:
            time.sleep(delay)
        return super().chat(body)


class _LibFakeAligner:
    name = "diag-fake"
    available = True

    def align(self, pcm: bytes, text: str, language: str) -> list:
        dur = len(pcm) / (2 * SAMPLE_RATE)
        words = "".join(ch if ch == "'" or ch.isalnum() else " "
                        for ch in text).split()
        if not words or dur <= 0:
            return []
        step = dur / (len(words) + 1)
        return [{"word": w, "start": round(step * i, 3),
                 "end": round(step * (i + 1), 3)} for i, w in enumerate(words)]


class Ledger:
    def __init__(self):
        self.lock = threading.Lock()
        self.wire = []
        self.stage = {}
        self.client = {"accepted_finals": [], "stale_rejects": [],
                       "wire_errors": [], "generation_gap_s": 0.0,
                       "discard_s": 0.0, "commit_ack_restarts": 0,
                       "last_commit_ack_wait": 0.0}

    def add(self, ev):
        with self.lock:
            self.wire.append(dict(ev))

    def bump(self, key, n=1):
        with self.lock:
            self.stage[key] = self.stage.get(key, 0) + n


def build_server(mock, aligner=None, ledger: Ledger = None):
    """Real app on loopback with SPAN-ONLY wire ledger (no texts)."""
    of = appmod.__dict__  # keep names local to avoid global churn
    from fourscribe import events as evmod
    ofinal, odelta, oerror, ocommit = (evmod.final_event, evmod.delta_event,
                                       evmod.error_event, evmod.committed_event)

    def final_event(text, processed, words, alignment):
        if ledger:
            ledger.add({"t": "final", "span": [0.0, processed],
                        "extra": {"words_n": len(words or [])}})
        return ofinal(text, processed, words, alignment)

    def error_event(code, message):
        if ledger:
            ledger.add({"t": "error", "code": code, "msg": message[:140]})
        return oerror(code, message)

    def committed_event(processed):
        if ledger:
            ledger.add({"t": "ack", "span": [0.0, processed]})
        return ocommit(processed)

    evmod.final_event = final_event
    evmod.error_event = error_event
    evmod.committed_event = committed_event

    cfg = Config(token="diag-token", ollama_url="http://mock",
                 min_endpointing_ms=800)
    app = appmod.create_app(
        cfg,
        lambda: httpx.Client(transport=mock.transport(), base_url=cfg.ollama_url),
        aligner=aligner or _LibFakeAligner())
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    return server, f"ws://127.0.0.1:{port}/4scribe/v1/audio/transcriptions/realtime"


def restore_events():
    """Reload events module so diagnostics never leak instrumentation."""
    import importlib
    import fourscribe.events as evmod
    importlib.reload(evmod)


class ReferenceClient:
    """Faithful algorithmic subset of remote.py. One PRODUCER feeds frames
    into a persistent queue; each ws SESSION consumes it; restart discards
    the queue (speech accounted) and the fresh session continues - no replay."""

    def __init__(self, url, token, frames_, ledger: Ledger):
        self.url, self.token = url, token
        self.frames = frames_
        self.L = ledger

    # ---- loop persistence ----
    async def run(self):
        q = asyncio.Queue(maxsize=520)
        st = {"queued_bytes": 0, "queued_speech": 0.0}
        producer = asyncio.create_task(self._producer(q, st))
        restarts = 0
        try:
            while True:
                ack = asyncio.Event()
                try:
                    async with websockets.connect(
                            self.url, open_timeout=15,
                            additional_headers={"authorization": "Bearer " + self.token}) as ws:
                        rcv = asyncio.create_task(self._receive(ws, ack, st))
                        try:
                            await self._send(ws, ack, q, st)
                        finally:
                            # faithful finish(): account the generation's
                            # unfinalized speech at session end (bounded
                            # finish window), other generations already
                            # consumed their spans at each final/epoch reset
                            self._account_restart(st)
                            rcv.cancel()
                            await asyncio.gather(rcv, return_exceptions=True)
                        return "completed"
                except Exception:
                    self._account_restart(st)
                    restarts += 1
                    if restarts > 20:
                        return "dead-lock-exit"
                    await asyncio.sleep(0.01)
        finally:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)

    def _account_restart(self, st):
        self.L.bump("generation_restarts")
        spans = st.pop("spans", []) or []
        lost = 0.0
        opens = st.pop("open_start", None)
        watermark = st.get("wm", 0.0)
        if opens is not None:
            lost += max(0.0, st.pop("open_end", 0.0) - max(opens, watermark))
        for s, e in spans:
            if e > watermark:
                lost += max(0.0, e - max(s, watermark))
        st["spans"] = []
        lost_d = st.pop("queued_speech", 0.0)
        st["queued_speech"] = 0.0
        self.L.client["generation_gap_s"] += round(lost, 3)
        self.L.client["discard_s"] += round(lost_d, 3)
        # faithful: drop the WHOLE queue, stream continues (no replay)
        while getattr(self, "_q_ref", None) and not self._q_ref.empty():
            item = self._q_ref.get_nowait()
            speech = item[3] if isinstance(item, tuple) and len(item) > 3 else 0.0
            self.L.client["discard_s"] += speech
        self.capture_point = getattr(self, "capture_point", 0)  # no-op marker

    # ---- producer: faithful feed() with byte budget and REALTIME pacing ----
    async def _producer(self, q, st):
        st.pop("open_start", None)
        st["open_end"] = 0.0
        sil_run_start = None
        last_commit = -1
        self._q_ref = q
        for pos, frame, speech in self.frames:
            await asyncio.sleep(0.02)  # faithful pacing (20 ms per frame)
            while (st.get("queued_bytes", 0) + len(frame) > QUEUE_SECONDS * 32000):
                # budget exceeded: restart marker (replay prohibited)
                self.L.bump("queue_exhaustion")
                self.L.client["generation_gap_s"] += speech
                await q.put(("reset", b"", pos, 0.0))
                return
            await q.put(("audio", frame, pos, speech))
            st["queued_bytes"] = st.get("queued_bytes", 0) + len(frame)
            st["queued_speech"] = st.get("queued_speech", 0) + speech
            self.L.bump("enqueued_frames")
            if speech > 0:
                self.L.bump("enqueued_speech_frames")
                if st.get("open_start") is None:
                    st["open_start"] = pos
                st["open_end"] = pos + 0.02
            else:
                if st.get("open_start") is not None:
                    # span closes on the FIRST non-speech frame (reference)
                    st.setdefault("spans", []).append((st["open_start"],
                                                       st["open_end"]))
                    st["open_start"] = None
                if sil_run_start is None:
                    sil_run_start = pos
                if pos - sil_run_start >= HANGOVER_S:
                    sil_run_start = None
                    if pos > last_commit:
                        last_commit = pos
                        await q.put(("commit", b"", pos, 0.0))
        if st.get("open_start") is not None:
            st.setdefault("spans", []).append((st["open_start"], st["open_end"]))
            st["open_start"] = None
        # reference flushes a final commit at stop
        await q.put(("commit", b"", self.frames[-1][0] + 0.02, 0.0))

    # ---- send loop (faithful fenced commits) ----
    async def _send(self, ws, ack, q, st):
        origin = None
        stream_end = 0.0
        while True:
            kind, pcm, pos, speech = await q.get()
            st["queued_bytes"] = max(0, st.get("queued_bytes", 0) - len(pcm))
            st["queued_speech"] = max(0.0, st.get("queued_speech", 0) - speech)
            self.L.bump("sent_frames")
            if speech > 0:
                self.L.bump("sent_speech_frames")
            if kind == "reset":
                raise RuntimeError("past the restart sink; drain in run()")
            if kind == "commit":
                await self._commit(ws, ack, st, origin, stream_end)
                origin, stream_end = None, 0.0
                st["wm"] = 0.0
                st["last_final_signature"] = None
                # faithful: the fresh epoch starts a clean span ledger
                st["spans"] = []
                continue
            if origin is None:
                origin = pos
                st["origin"] = pos
            stream_end = pos + len(pcm) / 32000.0
            await ws.send(pcm)
            if stream_end - origin >= FORCE_COMMIT_S:
                await self._commit(ws, ack, st, origin, stream_end)
                origin, stream_end = None, 0.0
                st["wm"] = 0.0
                st["last_final_signature"] = None
                st["spans"] = []

    async def _commit(self, ws, ack, st, origin, stream_end):
        self.L.bump("commits_sent")
        ack.clear()
        await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        began = time.monotonic()
        try:
            await asyncio.wait_for(ack.wait(), ACK_GRACE_S)
        except asyncio.TimeoutError:
            wait = time.monotonic() - began
            self.L.client["commit_ack_restarts"] += 1
            self.L.client["last_commit_ack_wait"] = round(wait, 3)
            raise CommitAckTimeout(f"commit ack {wait:.2f}s")

    # ---- receive (span-only ledger, acceptance exactly per reference) ----
    async def _receive(self, ws, ack, st):
        L = self.L
        await ws.send(json.dumps({"type": "session.update", "session": {
            "sample_rate": 16000, "language": "ru", "endpointing_ms": 800,
            "word_timestamps": True}}))
        upd = json.loads(await ws.recv())
        assert upd.get("type") == "session.updated", upd.get("type")
        L.bump("sessions_configured")
        while True:
            event = json.loads(await ws.recv())
            kind = event.get("type", "")
            if kind == "input_audio_buffer.committed":
                ack.set()
                L.bump("acks_seen")
                continue
            if kind == "error":
                L.client["wire_errors"].append(event.get("code"))
                L.bump("wire_errors")
                raise RuntimeError("Remote recognition failed")
            if kind.endswith(".completed"):
                processed = event.get("audio_processed", 0)
                words = event.get("words") or []
                if words:
                    start, end = words[0]["start"], words[-1]["end"]
                else:
                    start, end = 0.0, processed
                signature = (event.get("transcript") or "", round(end, 3))
                wm = st.get("wm", 0.0)
                if not (event.get("transcript") or "").strip() \
                        or signature == st.get("last_final_signature") \
                        or end <= wm - 1e-9:
                    L.client["stale_rejects"].append({"end": round(end, 3)})
                    L.bump("final_stale")
                    continue
                L.client["accepted_finals"].append((round(start, 3),
                                                    round(end, 3)))
                L.bump("final_accepted")
                st["wm"] = max(wm, end)
                st["last_final_signature"] = signature
                # faithful reference accounting: speech spans covered by the
                # new watermark are consumed (never double-counted later)
                spans = st.get("spans", [])
                st["spans"] = [(s, e) for s, e in spans
                               if e > st.get("origin", 0) + wm]
                if st.get("open_start") is not None and \
                        st.get("open_start", 0) < st.get("origin", 0) + wm:
                    st["open_start"] = None
            elif kind.endswith(".delta"):
                L.bump("delta_received")


class CommitAckTimeout(RuntimeError):
    pass


SCENARIO_SPECS = {
    "empty-decode": {
        "timeline": [("s", 1.0), (220, 1.4), ("s", 1.4), (330, 1.4),
                     ("s", 1.4), (220, 1.4), ("s", 3.0)],
        "delay": {"default": 0.6}, "empty_for": (330,),
    },
    "final-backlog-overflow": {
        "timeline": [("s", 0.6)] + [(220, 0.8), ("s", 0.8)] * 6 + [("s", 2.5)],
        "delay": {"default": 6.0}, "empty_for": (),
    },
    "forced-commit-slow-chain": {
        "timeline": [("s", 1.0), (220, 1.2), ("s", 2.2), (330, 30.5), ("s", 3.0)],
        "delay": {"default": 6.0}, "empty_for": (),
    },
}


def compose_frames(timeline):
    out = []
    t = 0.0
    for kind, seconds in timeline:
        if kind == "s":
            pcm = silence_pcm(seconds)
            speech = 0.0
        else:
            pcm = speech_pcm(seconds, freq=kind)
            speech = seconds
        pos = t
        for i in range(0, len(pcm), FRAME):
            out.append((pos, pcm[i:i + FRAME], speech))
            pos += 0.02
        t += seconds
    return out


def analyze(name, ledger: Ledger):
    merged = []
    for s, e in sorted(ledger.client["accepted_finals"]):
        if merged and s <= merged[-1][1] + 0.02:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return {
        "scenario": name,
        "speech_frames_captured": ledger.stage.get("enqueued_speech_frames", 0),
        "speech_frames_sent": ledger.stage.get("sent_speech_frames", 0),
        "finals_emitted_n": sum(1 for e in ledger.wire if e["t"] == "final"),
        "finals_accepted_n": len(ledger.client["accepted_finals"]),
        "accepted_final_spans_meeting_s": merged or [[0.0, 0.0]],
        "stale_rejects": ledger.client["stale_rejects"],
        "wire_errors": ledger.client["wire_errors"],
        "commit_ack_restarts": ledger.client.get("commit_ack_restarts", 0),
        "last_commit_ack_wait": ledger.client["last_commit_ack_wait"],
        "queue_exhaustion": ledger.stage.get("queue_exhaustion", 0),
        "generation_restarts": ledger.stage.get("generation_restarts", 0),
        "generation_gap_s": round(ledger.client["generation_gap_s"], 3),
        "discard_s": round(ledger.client["discard_s"], 3),
        "stage_counts": dict(ledger.stage),
        "server_events": ledger.wire,
    }


def run_scenario(name):
    spec = SCENARIO_SPECS[name]
    ledger = Ledger()
    mock = DiagOllama(delay_by_freq=spec["delay"], empty_for_freq=spec["empty_for"])
    server, url = build_server(mock, ledger=ledger)
    frames_ = compose_frames(spec["timeline"])
    client = ReferenceClient(url, "diag-token", frames_, ledger)
    timeout = spec.get("wall_budget_s", 90.0)
    try:
        try:
            asyncio.run(asyncio.wait_for(client.run(), timeout))
        except asyncio.TimeoutError:
            ledger.client["harness_timeout"] = round(timeout, 1)
        except CommitAckTimeout as exc:
            ledger.client["commit_ack_timeout"] = str(exc)
    finally:
        server.should_exit = True
    return analyze(name, ledger)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="all")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent))
    args = ap.parse_args()
    names = list(SCENARIO_SPECS) if args.scenario == "all" else [args.scenario]
    out = {n: run_scenario(n) for n in names}
    path = Path(args.out) / "commit_lifecycle_diagnosis.json"
    with open(path, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print("written", path)
    for name, rep in out.items():
        print("==", name)
        print(json.dumps({k: v for k, v in rep.items() if k != "server_events"},
                         ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
