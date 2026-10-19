"""Per-connection realtime session (isolated state per source).

State per WS connection; two sources never share buffers/decoders/hypotheses.
One decode IN-FLIGHT per session (the partial cadence starts the next decode
only after the previous completes, min interval 4 s). Inference AND alignment
run in worker pools behind explicit admission bounds, never on the event loop.
Cancelled jobs retain capacity until compute actually ends; obsolete results
are discarded by generation. Buffers are bounded; overflow is an explicit gap
event, never a silent drop.

Clock: every ACCEPTED PCM frame advances the absolute sample counter
(`epoch_samples`) - including chunks explicitly dropped under overflow - so
word times always refer to the original audio clock. Endpoints come from the
gate as absolute frame indices; a phrase's segment buffer is seeded exactly at
the speech-run start frame (onset preroll preserved) and cut exactly at the
endpoint frame, so identical PCM yields identical segment spans regardless of
transport chunk grouping.

Admission/backpressure (simple explicit bounds, no scheduler/broker):
- finalization jobs per session: FIFO capped at cfg.max_pending_finals; when
  full, the oldest waiting ENDPOINT final is dropped with an EXPLICIT error
  naming its dropped audio span (commits are never evicted while an endpoint
  final can be dropped instead);
- a single runner task per session executes admitted jobs in order, holding
  decode_lock through decode+align+emit, so emission order == decode order:
  endpoint finals queued before a commit always precede the ACK, the commit
  flush emits finals then the ACK then resets the epoch, and audio captured
  after the commit boundary is finalized in the NEW epoch - no late event
  crosses a commit and no admitted final text is silently discarded;
- global decode semaphore (cfg.inference_workers) bounds the shared executor's
  pending queue; the aligner has its own bounded semaphore/executor;
- outbound events go through a bounded queue with a sync never-blocking sink:
  stale provisional deltas may be dropped under backpressure (counted,
  superseded by later revisions); finals/ACK are never silently dropped.
"""
import asyncio
import logging
import secrets
import time

#: clock seam: patchable in tests without disturbing asyncio's own timers
monotonic = time.monotonic

logger = logging.getLogger("fscribe")

from . import events
from .aligner import AlignerUnavailable, AlignmentFault
from .asr import OllamaDecodeError
from .vad_gate import SpeechGate
from .verifier import NullVerifier
from .wavwrap import seconds_of

MIN_SPEECH_FOR_FINAL = 0.25  # ignore sub-onset blips for endpoint finals
MIN_SPEECH_FOR_PARTIAL = 0.8
PREROLL_RING_S = 0.35  # samples kept to seed a segment at its exact onset frame


class RealtimeSession:
    def __init__(self, cfg, asr, aligner, pool, sink, align_pool=None,
                 align_sem=None, decode_sem=None, tracer=None, verifier=None,
                 vad_pool=None):
        self.cfg = cfg
        self.asr = asr
        self.aligner = aligner
        # Shared pre-decode verifier. app.create_app injects the single
        # process-wide Silero verifier; direct construction defaults to the
        # accept-all NullVerifier (documented safe fallback, never a silent
        # rejection). Verification itself runs in vad_pool, never here.
        self.verifier = verifier or NullVerifier()
        self._vad_pool = vad_pool
        # optional metadata-only lifecycle tracer (see lifecycle_trace.py);
        # None by default: zero behavioural impact, no tracing cost
        self.tracer = tracer
        self._pool = pool
        self._align_pool = align_pool  # bounded executor for alignment
        self._align_sem = align_sem or asyncio.Semaphore(cfg.aligner_workers)
        self._decode_sem = decode_sem or asyncio.Semaphore(cfg.inference_workers)
        self._sink = sink  # SYNC callable(event_dict); must never block
        self.language = None  # must be explicitly set via session.update (ru/en)
        self.endpointing_ms = cfg.min_endpointing_ms
        self.word_timestamps_requested = False
        self._gate = SpeechGate(self.cfg.min_endpointing_ms, self.cfg.sample_rate)
        rate = cfg.sample_rate
        self._fs = self._gate.frame_samples  # gate frame size in samples
        # absolute ORIGINAL-audio clock: samples accepted since connection start
        self.epoch_samples = 0
        # absolute sample index where the current commit epoch begins
        self.epoch_start_samples = 0
        self.ring = bytearray()          # bounded preroll ring of recent samples
        self.ring_start = 0              # absolute sample index of ring[0]
        self.ring_misses = 0
        self.segbuf = bytearray()        # current segment recording buffer
        self.segbuf_start = 0            # absolute sample index of segbuf[0]
        self.segment_open = False
        self.last_final_pos = None  # per-epoch watermark of the emitted final
        self.generation = 1
        self.decode_lock = asyncio.Lock()  # held through decode+align+emit
        self.emit_lock = asyncio.Lock()
        self.final_jobs = []             # admitted, not-yet-run finalizations
        self.runner_task = None
        self.next_partial_at = 0.0
        self.overflow = False
        self.closed = False
        self.commit_pending = False  # a is_commit job admitted, not yet done
        self._pre_msg_start = 0
        self._partial_task = None
        self.stats = {"frames": 0, "partials": 0, "finals": 0,
                      "declined_decode": 0, "overflows": 0, "headline_errors": 0,
                      "dropped_deltas_backpressure": 0, "dropped_finals_backlog": 0,
                      "declined_stale_partial": 0, "ring_misses": 0,
                      "verifier_declines": 0, "verifier_faults": 0}

    # ---- clock helpers (epoch coordinates from the absolute sample clock) ----

    def _epoch_s(self, abs_samples: float) -> float:
        return (abs_samples - self.epoch_start_samples) / self.cfg.sample_rate

    # ---- inbound ----

    async def handle_binary(self, pcm: bytes):
        if self.closed:
            return
        self.stats["frames"] += 1
        usable = len(pcm) - (len(pcm) % 2)
        samples = usable // 2
        if not samples:
            return
        self._pre_msg_start = self.epoch_samples  # absolute start of this message
        if self.segment_open:
            self.segbuf.extend(pcm[:usable])
        prev_ring, prev_ring_start = bytes(self.ring), self.ring_start
        self._ring_extend(pcm[:usable])
        gate_ev = self._gate.feed(pcm[:usable])
        # every accepted frame advances the original audio clock, even content
        # explicitly dropped due to overflow (finding: exact epoch coordinates)
        self.epoch_samples += samples
        if self.tracer:
            # covers the overflow-drop path too: sample clock stays truthful
            self.tracer.record("pcm_accept", abs_start_samp=self._pre_msg_start,
                               abs_end_samp=self.epoch_samples, n=samples)

        if self.overflow:
            if gate_ev.speech_s > 0:
                self.overflow = False
                self._gate.reset_state()  # recovery re-onsets cleanly (see below)
                self.emit(events.error_event(
                    "segment_buffer_overflow",
                    "speech audio dropped while the segment buffer overflowed"))
            return  # dropped span was explicitly declared; nothing recorded here

        for run_start_f, end_f in gate_ev.closes:
            start_abs = run_start_f * self._fs
            end_abs = end_f * self._fs
            # every closed segment seeds exactly at its own speech-run start
            # frame, so segment spans are identical for any transport grouping
            self._seed_segment(start_abs, pcm, prev_ring, prev_ring_start)
            cut = (end_abs - self.segbuf_start) * 2
            seg_pcm = bytes(self.segbuf[:cut])
            rest = self.segbuf[cut:]
            seg_start_abs = self.segbuf_start
            self.segbuf = bytearray(rest)
            self.segbuf_start = end_abs
            self._admit_final(seg_pcm, end_abs, seg_start_abs, False)
        if gate_ev.opened is not None:
            start_abs = gate_ev.opened * self._fs
            if self.tracer:
                self.tracer.record("seg_open", abs_start_samp=start_abs)
            self._seed_segment(start_abs, pcm, prev_ring, prev_ring_start)
        if self.segment_open and gate_ev.opened is None and not gate_ev.hangover \
                and not gate_ev.closes:
            # nothing open and nothing pending: idle between phrases
            self.segment_open = False
            self.segbuf.clear()
        if self.segment_open:
            if self._buffer_seconds() >= self.cfg.buffer_cap_s:
                self._trigger_overflow()
            elif gate_ev.speech_s > 0:
                self.maybe_partial()

    def _seed_segment(self, start_abs: float, pcm: bytes, prev_ring: bytes,
                      prev_ring_start: int):
        """Ensure segbuf covers [start_abs, current end) exactly, seeded at the
        absolute onset sample (message bytes, recorded buffer, or preroll ring).
        The partial cadence deadline is initialized ONLY on a genuine segment
        start (first seed / new run), never on continuation frames that merely
        re-report the still-open run."""
        if self.segment_open and self.segbuf_start <= start_abs:
            self.segbuf = bytearray(self.segbuf[(start_abs - self.segbuf_start) * 2:])
        elif start_abs >= self._pre_msg_start:
            self.segbuf = bytearray(pcm[(start_abs - self._pre_msg_start) * 2:])
        else:
            off = (start_abs - prev_ring_start) * 2
            if off < 0:
                self.stats["ring_misses"] += 1
                off = 0
            usable = len(pcm) - (len(pcm) % 2)
            self.segbuf = bytearray(prev_ring[off:] + pcm[:usable])
        if self.segbuf_start != start_abs or not self.segment_open:
            self.next_partial_at = monotonic()  # genuine segment start
        self.segbuf_start = start_abs
        self.segment_open = True

    def _buffer_seconds(self):
        return len(self.segbuf) / (2 * self.cfg.sample_rate)

    def _ring_extend(self, pcm: bytes):
        self.ring.extend(pcm)
        cap = int(PREROLL_RING_S * self.cfg.sample_rate) * 2
        drop = len(self.ring) - cap
        if drop > 0:
            drop -= drop % 2
            del self.ring[:drop]
            self.ring_start += drop // 2

    def _ring_slice(self, abs_sample: int) -> bytes:
        off = (abs_sample - self.ring_start) * 2
        if off < 0:
            self.stats["ring_misses"] += 1
            off = 0
        return bytes(self.ring[off:])

    async def _verify_speech(self, pcm: bytes, *, kind: str) -> bool:
        """Shared pre-decode Silero check on the exact segment PCM.

        Never blocks the event loop (vad_pool, single worker + verifier lock).
        A runtime verifier fault fails OPEN: the audio is accepted so a broken
        verifier can never silently delete real speech. The fault is recorded
        server-side (stats/tracer/log) and deliberately NOT sent as a wire
        ``type:error``: the Loqui client treats every wire error as fatal.
        """
        v = self.verifier
        if v is None or not getattr(v, "enabled", False):
            return True
        loop = asyncio.get_running_loop()
        try:
            if self._vad_pool is not None:
                ok = await loop.run_in_executor(self._vad_pool, v.verify, pcm)
            else:
                ok = await loop.run_in_executor(None, v.verify, pcm)
        except Exception as exc:  # noqa: BLE001 - recoverable, never a wire error
            self.stats["verifier_faults"] += 1
            logger.warning("verifier fault (%s) during %s; continuing without "
                           "rejection", type(exc).__name__, kind)
            if self.tracer:
                self.tracer.record("verifier_fault", kind=kind,
                                   err_kind=type(exc).__name__)
            return True
        if not ok:
            self.stats["verifier_declines"] += 1
            if self.tracer:
                self.tracer.record("verifier_decline", kind=kind)
        return bool(ok)

    def _trigger_overflow(self):
        self.stats["overflows"] += 1
        self.overflow = True
        self.segment_open = False
        self.segbuf.clear()
        self._gate.reset_state()
        if self.tracer:
            self.tracer.record("buffer_overflow", cap_s=self.cfg.buffer_cap_s,
                               seg_s=round(self._buffer_seconds(), 3))
        self.emit(events.error_event(
            "segment_buffer_overflow",
            "segment buffer exceeded %ss; speech until this phrase end is "
            "intentionally not resumable from here" % self.cfg.buffer_cap_s))

    def maybe_partial(self):
        if self.decode_lock.locked() or monotonic() < self.next_partial_at:
            return
        if self.final_jobs:
            return  # keep the admitted work bounded while a final is pending
        if self._partial_task is not None and not self._partial_task.done():
            return  # one in-flight/queued partial per session
        if self._buffer_seconds() < MIN_SPEECH_FOR_PARTIAL:
            if monotonic() > self.next_partial_at:
                self.next_partial_at = monotonic() + 0.3
            return
        snapshot = bytes(self.segbuf)
        end_abs = self.epoch_samples
        self.next_partial_at = monotonic() + self.cfg.partial_interval_s
        self._partial_task = asyncio.create_task(self.partial_job(snapshot, end_abs))

    async def partial_job(self, pcm: bytes, end_abs: float):
        gen = self.generation
        epoch_start_snapshot = self.epoch_start_samples
        jid = secrets.token_hex(4)
        if self.tracer:
            self.tracer.record("partial_admit", job_id=jid, abs_end_samp=end_abs)
        try:
            async with self.decode_lock:
                if (gen != self.generation or self.closed
                        or self.epoch_start_samples != epoch_start_snapshot):
                    return  # stale before spending verifier/ASR work
                if not await self._verify_speech(pcm, kind="partial"):
                    return  # no partial text, no ASR, no aligner, buffer untouched
                text = await self.run_decode(pcm, gen, is_partial=True, job_id=jid)
                if (gen != self.generation or self.closed
                        or self.epoch_start_samples != epoch_start_snapshot):
                    return  # stale: cancelled, closed, or a commit boundary crossed
                if not text.strip():
                    return
                processed = self._epoch_s(end_abs)
                if self.last_final_pos is not None and processed <= self.last_final_pos:
                    return  # never emit a non-advancing delta
                async with self.emit_lock:
                    if (gen != self.generation or self.closed
                            or self.epoch_start_samples != epoch_start_snapshot):
                        return  # no obsolete partial across a commit boundary
                    processed = self._epoch_s(end_abs)
                    if self.last_final_pos is not None and processed <= self.last_final_pos:
                        return
                    self.stats["partials"] += 1
                    if self.tracer:
                        self.tracer.record("partial_emit", job_id=jid,
                                           processed_s=self._epoch_s(end_abs))
                    self.emit(events.delta_event(text, processed))
        finally:
            self._partial_task = None

    async def run_decode(self, pcm: bytes, gen: int, *, is_partial: bool = False,
                         job_id: str = ""):
        """Blocking decode in the shared bounded pool; global admission
        semaphore keeps the executor's pending queue bounded; a cancelled or
        closed session yields without consuming compute."""
        if self.language is None:
            self.stats["declined_decode"] += 1
            if self.tracer:
                self.tracer.record("decode_declined", job_id=job_id,
                                   reason="not_configured")
            self.emit(events.error_event("session_not_configured",
                                         "explicit language not applied yet; decode declined"))
            return ""
        async with self._decode_sem:
            if gen != self.generation or self.closed:
                return ""
            if is_partial and self.commit_pending:
                # Race fix (owner/Codex instruction): a partial whose barrier
                # snapshot predates a COMMIT admission must not submit its
                # stale executor decode once a slot frees - the commit job
                # waits FIFO behind it and the live fence would burn its
                # whole budget on never-deliverable work. Not-yet-started
                # work only; already-submitted compute is never cancelled
                # and always runs to completion with capacity retained.
                self.stats["declined_stale_partial"] += 1
                if self.tracer:
                    self.tracer.record("decode_declined", job_id=job_id,
                                       reason="stale_partial_pending_commit")
                return ""
            if self.tracer:
                self.tracer.record("decode_submit", job_id=job_id,
                                   partial=is_partial)
            try:
                res = await asyncio.get_running_loop().run_in_executor(
                    self._pool, self.asr.decode, pcm, self.language)
                if self.tracer:
                    self.tracer.record("decode_end", job_id=job_id)
                return res.text
            except OllamaDecodeError as exc:
                if self.tracer:
                    self.tracer.record("decode_error", job_id=job_id,
                                       err_kind=type(exc).__name__)
                self.emit(events.error_event("decode_error", str(exc)[:160]))
                return ""
            except Exception as exc:  # noqa: BLE001
                if self.tracer:
                    self.tracer.record("decode_error", job_id=job_id,
                                       err_kind=type(exc).__name__)
                self.emit(events.error_event("decode_error", type(exc).__name__))
                return ""

    async def _align_async(self, pcm: bytes, text: str):
        """Alignment in the bounded aligner pool; capacity retained until the
        underlying compute ends (semaphore + executor), faults are explicit."""
        if self.language is None:
            return [], {"status": "fault", "reason": "aligner_unavailable"}
        async with self._align_sem:
            loop = asyncio.get_running_loop()
            try:
                fn = (lambda: self.aligner.align(pcm, text, self.language))
                words = await loop.run_in_executor(self._align_pool, fn)
                return list(words or []), {"status": "ok", "backend": self.aligner.name}
            except AlignerUnavailable as exc:
                return [], {"status": "fault", "reason": "aligner_unavailable",
                            "detail": str(exc)[:120]}
            except AlignmentFault as exc:
                return [], {"status": "fault", "reason": str(exc)[:120]}
            except Exception as exc:  # noqa: BLE001
                return [], {"status": "fault", "reason": type(exc).__name__}

    # ---- finalization admission (bounded FIFO + single ordered runner) ----

    def _admit_final(self, pcm: bytes, end_abs: float, seg_start_abs: float,
                     is_commit: bool):
        jid = secrets.token_hex(4)
        if self.tracer:
            self.tracer.record("job_admit", job_id=jid,
                               job_kind="commit" if is_commit else "final",
                               abs_start_samp=seg_start_abs,
                               abs_end_samp=end_abs)
        job = (pcm, end_abs, seg_start_abs, is_commit, jid)
        if len(self.final_jobs) >= self.cfg.max_pending_finals:
            evicted = None
            for i, j in enumerate(self.final_jobs):
                if not j[3]:  # evict the oldest waiting ENDPOINT final first
                    evicted = self.final_jobs.pop(i)
                    break
            if evicted is None:
                evicted = self.final_jobs.pop(0)  # all commits queued: drop oldest commit (explicit)
            if evicted[3] and not any(j[3] for j in self.final_jobs):
                # the evicted COMMIT was the last pending one: nothing remains
                # to hold the fence; partials must not be blocked forever
                self.commit_pending = False
            self.stats["dropped_finals_backlog"] += 1
            if self.tracer:
                self.tracer.record("job_evict_backlog", job_id=evicted[4],
                                   job_kind="commit" if evicted[3] else "final",
                                   abs_start_samp=evicted[2],
                                   abs_end_samp=evicted[1])
            self.emit(events.error_event(
                "final_backlog_overflow",
                "finalization backlog exceeded %d; dropped audio span [%s, %s]s "
                "without a final" % (self.cfg.max_pending_finals,
                                     round(self._epoch_s(evicted[2]), 3),
                                     round(self._epoch_s(evicted[1]), 3))))
        if is_commit:
            self.commit_pending = True
        self.final_jobs.append(job)
        self._ensure_runner()

    def _ensure_runner(self):
        if self.runner_task is None or self.runner_task.done():
            self.runner_task = asyncio.create_task(self._final_runner())

    async def _final_runner(self):
        while self.final_jobs and not self.closed:
            pcm, end_abs, seg_start_abs, is_commit, jid = self.final_jobs.pop(0)
            if self.tracer:
                self.tracer.record("job_start", job_id=jid,
                                   job_kind="commit" if is_commit else "final")
            try:
                await self._finalize(pcm, end_abs, seg_start_abs, is_commit, jid)
            except Exception as exc:  # noqa: BLE001 - explicit, never silent
                if self.tracer:
                    self.tracer.record("finalizer_error", job_id=jid,
                                       err_kind=type(exc).__name__)
                self.emit(events.error_event("finalizer_error",
                                             f"{type(exc).__name__}: {exc}"[:160]))
            finally:
                if is_commit:
                    # the commit job HAS finished (or failed): future partial
                    # scheduling must not be blocked by a job that can no
                    # longer affect the fence (pop-time clearing would break
                    # the stale-partial guard while the job is still waiting
                    # behind in-flight work)
                    self.commit_pending = False

    async def _finalize(self, pcm: bytes, end_abs: float, seg_start_abs: float,
                        is_commit: bool, jid: str = ""):
        gen = self.generation
        async with self.decode_lock:
            text = ""
            if is_commit and not pcm:
                # An EMPTY-buffer commit (always the live case after an endpoint
                # drained segbuf, or pure silence since the last phrase) NEEDS
                # only the ACK: decoding an empty buffer previously produced a
                # decode_error wire event, which the REAL client treats as
                # fatal for the whole generation (restart WITHOUT replay ->
                # middle phrases dropped in the live meeting). Fatal treatment
                # stays for genuine decodes; empty-buffer commits just ACK.
                pass
            elif self.language is not None and not self.closed and gen == self.generation:
                if await self._verify_speech(pcm, kind="final"):
                    text = await self.run_decode(pcm, gen, job_id=jid)
                else:
                    text = ""  # reject: no ASR, no aligner; commit ACK still runs below
            if gen != self.generation or self.closed:
                return  # cancelled/voided while compute ran (capacity was retained)
            if text.strip() and speech_present(pcm, self._gate):
                words, align_meta = [], {"status": "not_requested"}
                if self.word_timestamps_requested:
                    words, align_meta = await self._align_async(pcm, text)
                self.stats["finals"] += 1
                async with self.emit_lock:
                    if gen != self.generation or self.closed:
                        return
                    # words map from the EXACT segment buffer start (absolute
                    # original-audio samples) into current epoch coordinates
                    base = self._epoch_s(seg_start_abs)
                    mapped = [{"word": w["word"],
                               "start": round(base + w["start"], 3),
                               "end": round(base + w["end"], 3)} for w in words]
                    processed = self._epoch_s(end_abs)
                    if self.tracer:
                        self.tracer.record("final_emit", job_id=jid,
                                           span_start_s=base,
                                           span_end_s=processed,
                                           n_aligned=len(mapped),
                                           align_status=(align_meta or {}).get("status"))
                    self.emit(events.final_event(text, processed, mapped, align_meta))
                    self.last_final_pos = processed
            else:
                self.stats["declined_decode"] += 1
                if self.tracer:
                    self.tracer.record("final_no_speech_decline", job_id=jid,
                                       span_start_s=self._epoch_s(seg_start_abs),
                                       span_end_s=self._epoch_s(end_abs))
            if is_commit:
                async with self.emit_lock:
                    if gen != self.generation or self.closed:
                        return
                    # ACK strictly after every epoch final was emitted (decode
                    # order == emission order); then reset the epoch clock
                    if self.tracer:
                        self.tracer.record("commit_ack", job_id=jid)
                    self.emit(events.committed_event(0.0))
                    self.last_final_pos = None
                    self.epoch_start_samples = end_abs

    async def handle_commit(self):
        if self.closed:
            return
        end_abs = self.epoch_samples
        seg_start_abs = self.segbuf_start if self.segment_open else end_abs
        pcm = bytes(self.segbuf) if self.segment_open else b""
        self.segbuf.clear()
        self.segment_open = False
        self._gate.reset_state()
        self._admit_final(pcm, end_abs, seg_start_abs, True)

    async def handle_cancel(self):
        self.generation += 1  # late results are discarded on compute completion
        self.final_jobs.clear()  # cancel is an explicit abort of queued work
        self.commit_pending = False

    async def handle_session_update(self, payload):
        sess = (payload or {}).get("session") or {}
        applied = {}
        err = None
        sample_rate = sess.get("sample_rate")
        if sample_rate and sample_rate != self.cfg.sample_rate:
            err = {"code": "invalid_sample_rate", "message": "only 16 kHz PCM16 mono accepted"}
        language = (sess.get("language") or "").lower()
        if language:
            if language not in self.cfg.language_options:
                err = {"code": "unsupported_language",
                       "message": "autodetect is not accepted; only ru/en"}
            else:
                self.language = language
                applied["language"] = language
        ep = sess.get("endpointing_ms")
        if ep is not None:
            self.endpointing_ms = max(int(ep), self.cfg.min_endpointing_ms)
            applied["endpointing_ms"] = self.endpointing_ms
            self._gate.set_endpointing_ms(self.endpointing_ms)
        want_words = bool(sess.get("word_timestamps"))
        self.word_timestamps_requested = want_words
        # truthful capability: applied only when the real aligner is present
        applied["words"] = bool(want_words and self.aligner.available)
        if want_words and not self.aligner.available:
            err = err or {"code": "word_timestamps_unavailable",
                          "message": "aligner unavailable; finals will carry empty words + fault"}
        result = events.session_updated({"type": "session.update", "session": sess}, applied, err)
        self.emit(result)

    async def close(self):
        self.closed = True
        self.generation += 1  # late results after stop are void by generation guard
        self.commit_pending = False  # stop must not block anything afterwards
        if self.tracer:
            self.tracer.record("session_close",
                               **{"n_" + k: v for k, v in self.stats.items()})

    def emit(self, ev):
        if self.closed:
            return
        # SYNC non-blocking sink: no producer tasks, bounded queue in the app
        self._sink(ev)


def speech_present(pcm: bytes, gate: SpeechGate) -> bool:
    """Cheap energy check over the buffered tail: never decode dead silence."""
    if seconds_of(len(pcm), gate.rate) < MIN_SPEECH_FOR_FINAL:
        return False
    step = gate.frame_samples * 2
    for i in range(0, len(pcm), step):
        if gate.feed_speech_seconds(pcm[i:i + step]) > 0:
            return True
    return False
