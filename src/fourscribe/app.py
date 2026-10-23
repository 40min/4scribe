"""4scribe FastAPI application. Loopback-only realtime WSS/WS + health/ready.

Auth: Authorization: Bearer <token>. The default is a TEST token (tests supply
their own); FSCRIBE_TOKEN is only for local/non-live use - the deployed
production Bearer is provisioned via the owner-approved deployment plan, never
here. The mobile batch endpoint uses a SEPARATE Bearer (FSCRIBE_MOBILE_TOKEN,
see batch.py / docs/mobile-batch-api.md); it never accepts the realtime token.
Logs must not store audio samples, transcripts or tokens.

Bounded delivery: outbound events go through BoundedEventQueue (ordered,
bounded, sync never-blocking put). Under backpressure stale provisional deltas
may be dropped (counted, superseded by later revisions/finals); finals and
commit ACKs are never silently dropped (transient over-limit is counted and
bounded by final admission). Inference admission: one global decode semaphore
+ per-session final admission keep the shared executors' pending queues
bounded; cancellation retains capacity while compute still runs.
"""
import asyncio
import collections
import concurrent.futures
import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from .aligner import get_aligner
from .asr import OllamaChat
from .batch import BATCH_HTTP_PATHS, BatchTranscriber
from .config import Config
from .enhancement import TEXT_ENHANCE_PATHS, TextEnhancer
from .lifecycle_trace import new_session_tracer
from .session import RealtimeSession
from .verifier import get_verifier

logger = logging.getLogger("fscribe")

# Additive route register: the plan-doc loopback path plus the tailnet-prefixed
# path served by the Tailscale handler /4scribe (Serve proxies the original
# path verbatim, so the app itself answers under the prefix; no proxy spec and
# the /v1 route stays byte-for-byte unchanged).
REALTIME_WS_PATHS = (
    "/v1/audio/transcriptions/realtime",
    "/4scribe/v1/audio/transcriptions/realtime",
)


class BoundedEventQueue:
    """Ordered event queue with a HARD finite bound and explicit overload.

    put() is sync and NEVER blocks (no producer tasks pile up). Policy:
    - nonoptional events preserve their order and precede queued enhancements;
    - optional enhancement results may be dropped/evicted without overload;
    - a stale provisional delta arriving at capacity is dropped (counted);
      the next revision/final supersedes it;
    - a final/ACK/error at capacity first evicts droppable deltas to make room;
    - if no delta can be evicted the HARD bound is enforced: the event is NOT
      appended, the queue never exceeds maxsize, the overload is counted and
      logged, and the one-shot on_overload callback closes the session
      explicitly (stalled reader / stalled ws.send must not keep the session
      pretending healthy). No silent final/ACK discard, no unbounded growth.
    """

    def __init__(self, maxsize: int = 512, on_overload=None):
        self.maxsize = max(maxsize, 1)
        self._q = collections.deque()
        self._ready = asyncio.Event()
        self.dropped_enhancements = 0
        self.dropped_deltas = 0
        self.overflows = 0
        self.overload_fired = False
        self._on_overload = on_overload

    @staticmethod
    def _is_delta(ev):
        return (ev.get("type") or "").endswith(".delta")

    @staticmethod
    def _is_optional(ev):
        return ev.get("type") == "conversation.item.input_audio_transcription.enhanced"

    def put(self, ev):
        if len(self._q) >= self.maxsize:
            if self._is_optional(ev):
                self.dropped_enhancements += 1
                return
            for i, queued in enumerate(self._q):
                if self._is_optional(queued):
                    del self._q[i]
                    self.dropped_enhancements += 1
                    self._append(ev)
                    self._ready.set()
                    return
            if self._is_delta(ev):
                self.dropped_deltas += 1
                logger.warning("outbound backpressure: dropped provisional delta "
                               "(total %d)", self.dropped_deltas)
                return
            for i, e in enumerate(self._q):
                if self._is_delta(e):
                    del self._q[i]
                    self.dropped_deltas += 1
                    self._append(ev)
                    self._ready.set()
                    return
            # hard bound reached, nothing droppable: explicit overload
            self.overflows += 1
            if not self.overload_fired:
                self.overload_fired = True
                logger.error("outbound queue hard overflow (%d events); "
                             "session overload -> explicit close", len(self._q))
                if self._on_overload is not None:
                    self._on_overload()
            return
        self._append(ev)
        self._ready.set()

    def _append(self, ev):
        # Already-sending events cannot be preempted, but pending optional
        # corrections must never sit ahead of a raw final, ACK or other event.
        if not self._is_optional(ev):
            for i, queued in enumerate(self._q):
                if self._is_optional(queued):
                    self._q.insert(i, ev)
                    return
        self._q.append(ev)

    def discard_enhancements(self):
        """Discard optional corrections invalidated by cancel/disable/close."""
        self._q = collections.deque(e for e in self._q if not self._is_optional(e))

    async def get(self):
        while not self._q:
            self._ready.clear()
            if not self._q:
                await self._ready.wait()
        return self._q.popleft()

    def __len__(self):
        return len(self._q)


def create_app(cfg: Config = None, http_factory=None, aligner=None, verifier=None,
               enhancement_http_factory=None):
    cfg_provided = cfg is not None
    cfg = cfg or Config.from_env()
    if not cfg_provided:
        # Production startup guard (deployment PLAN): the service may NEVER
        # start live with the default TEST token. Tests construct Config
        # explicitly and are unaffected; an env-driven launch (systemd unit,
        # bare uvicorn) with a missing/default token fails closed here.
        # Local non-live test launches must set FSCRIBE_ALLOW_TEST_TOKEN=1.
        if (cfg.token or "").strip() in ("", "test-token") \
                and os.environ.get("FSCRIBE_ALLOW_TEST_TOKEN") != "1":
            raise RuntimeError(
                "fourscribe startup guard: FSCRIBE_TOKEN is unset or set to the "
                "default test token. Live deployment must supply the provisioned "
                "FSCRIBE_TOKEN (from /opt/40zen/secrets/4scribe.env). Local test "
                "launches may set FSCRIBE_ALLOW_TEST_TOKEN=1.")
    asr = OllamaChat(cfg, http=http_factory() if http_factory else None)
    aligner = aligner or get_aligner(cfg)
    # One shared verifier for every session: the verifier lock + single-worker
    # executor serialize Silero state reset/inference across streams.
    verifier = verifier if verifier is not None else get_verifier(cfg)
    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=cfg.inference_workers, thread_name_prefix="fscribe-decode")
    align_pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=cfg.aligner_workers, thread_name_prefix="fscribe-align")
    vad_pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="fscribe-vad")
    align_sem = asyncio.Semaphore(cfg.aligner_workers)
    decode_sem = asyncio.Semaphore(cfg.inference_workers)

    # Standalone and inline text correction share one admission slot and async
    # HTTP client; it never takes the ASR decode semaphore, so correction and
    # transcription are not serialized by an application lock. The async client
    # is created on startup and closed on shutdown, never at module import.
    enhancer = TextEnhancer(cfg, client_factory=enhancement_http_factory)

    @asynccontextmanager
    async def lifespan(_app):
        await enhancer.start()
        try:
            yield
        finally:
            await enhancer.close()

    app = FastAPI(title="4scribe", version="0.1.0", lifespan=lifespan)

    # Mobile batch API (HardAfanasii). Reuses the app's executor, decode
    # semaphore and single-pool Silero verifier; disabled (503) unless a
    # separate FSCRIBE_MOBILE_TOKEN is configured. See batch.py.
    batch = BatchTranscriber(cfg, asr=asr, pool=pool, decode_sem=decode_sem,
                             verifier=verifier, vad_pool=vad_pool, enhancer=enhancer)
    app.state.batch_transcriber = batch
    app.state.decode_pool = pool
    app.state.decode_sem = decode_sem
    app.state.verifier = verifier
    app.state.text_enhancer = enhancer

    async def _healthz_impl():
        return {"ok": True, "aligner": aligner.name,
                "verifier": verifier.name,
                "word_timestamps": bool(aligner.available),
                "mobile_batch": batch.status(),
                "text_enhancement": enhancer.status()}

    async def _readyz_impl():
        # light readiness: loopback Ollama reachable via /api/tags; no model warm-up
        ok = await asyncio.get_running_loop().run_in_executor(pool, asr.reachable)
        return {"ready": ok, "aligner": aligner.name,
                "word_timestamps": bool(aligner.available),
                "workers": cfg.inference_workers,
                "mobile_batch": batch.status()}

    # Root pair (existing contract) + identical aliases under the served
    # /4scribe prefix so health checks answer whichever verbatim-prefix mode
    # Tailscale Serve applies; no proxy or infra change.
    for hpath in ("/healthz", "/4scribe/healthz"):
        app.get(hpath)(_healthz_impl)
    for rpath in ("/readyz", "/4scribe/readyz"):
        app.get(rpath)(_readyz_impl)

    async def realtime(ws: WebSocket):
        auth = ws.headers.get("authorization") or ""
        if auth != "Bearer " + cfg.token:
            await ws.close(code=1008)
            return
        await ws.accept()
        ended = asyncio.Event()
        outbound = BoundedEventQueue(maxsize=cfg.outbound_queue_max)

        def sink(ev):
            outbound.put(ev)  # sync, never blocks: no producer tasks

        # opt-in metadata-only lifecycle trace (FSCRIBE_LIFECYCLE_TRACE=1);
        # None and zero-cost when disabled (see lifecycle_trace.py)
        tracer = new_session_tracer()

        session = RealtimeSession(cfg, asr, aligner, pool, sink,
                                  align_pool=align_pool, align_sem=align_sem,
                                  decode_sem=decode_sem, tracer=tracer,
                                  verifier=verifier, vad_pool=vad_pool, enhancer=enhancer,
                                  discard_enhancements=outbound.discard_enhancements)

        # HARD overload path: one bounded shutdown task (never per event);
        # must not depend on the possibly stalled pump.
        overload_scheduled = False

        def on_overload():
            nonlocal overload_scheduled
            if overload_scheduled:
                return
            overload_scheduled = True
            if tracer is not None:
                tracer.record("outbound_overload", held_events=len(outbound))
            asyncio.get_running_loop().create_task(_overload_close())

        async def _overload_close():
            session.closed = True  # stops emits; late results voided below
            ended.set()
            pump_task.cancel()
            try:
                await asyncio.wait_for(ws.close(code=1013), 1.0)
            except Exception:  # noqa: BLE001 - stall/dead socket; loop cleanup follows
                pass

        outbound._on_overload = on_overload

        async def pump():
            while not ended.is_set():
                ev = await outbound.get()
                try:
                    await ws.send_text(json.dumps(ev))
                except Exception:  # noqa: BLE001 - peer gone/stalled; stop the pump
                    session.closed = True
                    ended.set()
                    return

        config_done = asyncio.Event()
        pump_task = asyncio.create_task(pump())

        async def first_update_gate(payload):
            await session.handle_session_update(payload)
            config_done.set()

        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if "text" in msg and msg["text"] is not None:
                    try:
                        payload = json.loads(msg["text"])
                    except ValueError:
                        sink({"type": "error", "event_id": "e-malformed-json",
                              "code": "malformed_frame", "message": "text frame is not JSON"})
                        continue
                    kind = payload.get("type")
                    if kind == "session.update":
                        await first_update_gate(payload)
                    elif kind == "input_audio_buffer.commit":
                        await session.handle_commit()
                    elif kind == "response.cancel":
                        await session.handle_cancel()
                    elif kind == "error":
                        continue
                    else:
                        sink({"type": "error", "event_id": "e-unknown",
                              "code": "unknown_type",
                              "message": f"unexpected type {kind!r}"})
                elif "bytes" in msg and msg["bytes"] is not None:
                    await session.handle_binary(msg["bytes"])
        except WebSocketDisconnect:
            pass
        finally:
            ended.set()
            await session.close()
            t = getattr(session, "tracer", None)
            if t is not None:
                t.close()  # final bounded flush; writer thread joins
            pump_task.cancel()
            try:
                await asyncio.gather(pump_task, return_exceptions=True)
            except Exception:  # noqa: BLE001
                pass
            # late decode results after stop are void by session.generation guard.

    for path in REALTIME_WS_PATHS:
        app.add_api_websocket_route(path, realtime)

    # Ordinary HTTP multipart batch endpoint (NOT streaming), prefixed route
    # plus the loopback alias mirroring the health/WS pattern above. No legacy
    # /parakeet-test URL is served or preserved here.
    for path in BATCH_HTTP_PATHS:
        app.post(path)(batch.handle)

    # Separate standalone text-correction API (Loqui); no batch/session path
    # or legacy /api/text/enhance alias is added here.
    for path in TEXT_ENHANCE_PATHS:
        app.post(path)(enhancer.handle)

    return app
