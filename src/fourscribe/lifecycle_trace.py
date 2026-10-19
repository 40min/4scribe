"""Opt-in, metadata-only lifecycle trace for realtime sessions.

PURPOSE: close the FIRST-LOST-STAGE evidence gap on the live middle-of-meeting
speech losses (handoff 2026-10-17). When a phrase is lost the gap metric only
reports a total; this trace records the actual stage chain per event so the
first stage where speech stopped progressing is visible.

PRIVACY (hard): an explicit PER-STAGE ALLOWLIST governs what may be recorded —
there is no denylist fallback. Only connections' random ids, relative
monotonic times, accepted-PCM SAMPLE offsets, job kinds/spans, queue depths,
alignment STATUS (from a fixed enum), bounded error-kind strings (identifier
pattern only) and bounded-queue counters pass. Unknown fields, unknown stage
names, non-finite floats and over-long/identifier-violating strings are
DROPPED, never truncated-through. No audio samples, transcripts, tokens,
headers, env or paths are ever attached.

BOUNDS / STORAGE:
- In-memory FIFO ring per connection (default 2000 records), eviction counted.
- Each WS connection writes to its OWN file: <configured-path-stem>-<conn_id>
  .jsonl (no shared file, no shared .tmp). The number of files therefore
  equals the number of WS connections during the trace window and each file
  holds at most max_records records — total storage is connections ×
  max_records × ~a few hundred bytes. Collection/archival of per-connection
  files is an explicit operator action (see TRACE-REPLAY-HANDOFF.md); nothing
  here grows indefinitely beyond that documented model.
- Writes happen on a daemon writer thread serialized by a write lock
  (concurrent close + writer flush is safe). record() itself never blocks the
  event loop and never raises.

OPT-IN ONLY: FSCRIBE_LIFECYCLE_TRACE=1 via new_session_tracer(). Source-only;
activation is a separate owner-approved restart. stdlib only.
"""
import json
import math
import os
import re as _re
import secrets
import threading
import time
from collections import deque
from pathlib import Path

DEFAULT_MAX_RECORDS = 2000
DEFAULT_FLUSH_INTERVAL_S = 0.25
MAX_STR_LEN = 64

#: constrained stage (record) names — every record() call must match
TRACE_STAGES = frozenset({
    "pcm_accept", "seg_open", "buffer_overflow",
    "job_admit", "job_evict_backlog", "job_start", "finalizer_error",
    "decode_submit", "decode_end", "decode_declined", "decode_error",
    "partial_admit", "partial_emit",
    "final_emit", "final_no_speech_decline", "commit_ack",
    "outbound_overload", "session_close",
})

_JOB_KINDS = ("commit", "final")
_ALIGN_STATUS = ("ok", "fault", "not_requested")
_DECODE_DECLINE = ("not_configured", "stale_partial_pending_commit")
_ERR_KIND_PATTERN = _re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,64}$")
_ID_PATTERN = _re.compile(r"^[0-9a-fA-F]{4,32}$")
_STATS_BITS = ("frames", "partials", "finals", "declined_decode", "overflows",
               "headline_errors", "dropped_deltas_backpressure",
               "dropped_finals_backlog", "declined_stale_partial",
               "ring_misses")

#: value validators per (stage, field): 'int' | 'float' | 'bool' | 'enum:…'
#: | 'id' (hex tokens) | 'identifier' (error class names)
_SPECS = {
    "pcm_accept": {"abs_start_samp": "int", "abs_end_samp": "int", "n": "int"},
    "seg_open": {"abs_start_samp": "int"},
    "buffer_overflow": {"cap_s": "float", "seg_s": "float"},
    "job_admit": {"job_id": "id", "job_kind": "enum:commit|final",
                  "abs_start_samp": "int", "abs_end_samp": "int"},
    "job_evict_backlog": {"job_id": "id", "job_kind": "enum:commit|final",
                          "abs_start_samp": "int", "abs_end_samp": "int"},
    "job_start": {"job_id": "id", "job_kind": "enum:commit|final"},
    "finalizer_error": {"job_id": "id", "err_kind": "identifier"},
    "decode_submit": {"job_id": "id", "partial": "bool"},
    "decode_end": {"job_id": "id"},
    "decode_declined": {"job_id": "id", "reason": "enum:%s" % "|".join(_DECODE_DECLINE)},
    "decode_error": {"job_id": "id", "err_kind": "identifier"},
    "partial_admit": {"job_id": "id", "abs_end_samp": "int"},
    "partial_emit": {"job_id": "id", "processed_s": "float"},
    "final_emit": {"job_id": "id", "span_start_s": "float", "span_end_s": "float",
                   "n_aligned": "int", "align_status": "enum:%s" % "|".join(_ALIGN_STATUS)},
    "final_no_speech_decline": {"job_id": "id", "span_start_s": "float",
                                "span_end_s": "float"},
    "commit_ack": {"job_id": "id"},
    "outbound_overload": {"held_events": "int"},
    "session_close": dict(("n_" + k, "int") for k in _STATS_BITS),
}


def _sanitize_fields(stage: str, fields: dict):
    """Explicit allowlist per stage: unknown fields/stages are dropped whole,
    invalid enum/identifier/id values are dropped, non-finite floats are
    dropped. Returns (clean_fields, dropped_names)."""
    spec = _SPECS.get(stage)
    if spec is None:
        return {}, tuple((fields or {}).keys())
    out = {}
    dropped = []
    for k, v in (fields or {}).items():
        rule = spec.get(k)
        if rule is None:
            dropped.append(k)
            continue
        if rule == "int":
            if isinstance(v, bool) or not isinstance(v, int):
                dropped.append(k)
            else:
                out[k] = v
        elif rule == "float":
            if isinstance(v, bool) or not isinstance(v, (int, float)) \
                    or not math.isfinite(float(v)):
                dropped.append(k)
            else:
                out[k] = round(float(v), 4)
        elif rule == "bool":
            if isinstance(v, bool):
                out[k] = v
            else:
                dropped.append(k)
        elif rule.startswith("enum:"):
            if isinstance(v, str) and v in frozenset(rule[5:].split("|")):
                out[k] = v
            else:
                dropped.append(k)
        elif rule == "id":
            if isinstance(v, str) and _ID_PATTERN.match(v) \
                    and all(c in "0123456789abcdef" for c in v):
                out[k] = v
            else:
                dropped.append(k)
        elif rule == "identifier":
            if isinstance(v, str) and _ERR_KIND_PATTERN.match(v):
                out[k] = v[:MAX_STR_LEN]
            else:
                dropped.append(k)
        else:  # defensively unreachable
            dropped.append(k)
    return out, tuple(dropped)


class SessionTracer:
    """One trace per WS connection. Sync non-blocking, sanitized record into a
    bounded ring; per-connection file written by a daemon thread."""

    def __init__(self, file_dir_or_path: str = None, max_records: int = None,
                 flush_interval_s: float = None):
        self.conn_id = secrets.token_hex(8)
        self.t0 = time.monotonic()
        self.max_records = max(max_records or DEFAULT_MAX_RECORDS, 1)
        self.flush_interval_s = max(flush_interval_s or DEFAULT_FLUSH_INTERVAL_S, 0.05)
        self._seq = 0                     # mutation counter, monotonic forever
        self.evicted = 0                  # records dropped by the FIFO bound
        self.write_failures = 0
        self._unwritten_seq = 0           # seq reflected in the last file write
        self.file_path = None
        self._records = deque(maxlen=self.max_records)
        self._lock = threading.Lock()     # protects ring/counters
        self._write_lock = threading.RLock()  # serializes file I/O (+ close)
        self._stopped = threading.Event()
        if file_dir_or_path:
            self.file_path = self._per_connection_path(file_dir_or_path)
            self._writer = threading.Thread(
                target=self._write_loop, daemon=True,
                name=f"fscribe-lifecycle-trace-{self.conn_id}")
            self._writer.start()

    def _per_connection_path(self, configured: str) -> str:
        """Unique file per connection: stem-conn_id.suffix (no shared file, no
        shared tmp). Falls back to memory-only if the path is unusable."""
        p = Path(configured)
        unique = p.with_name(f"{p.stem}-{self.conn_id}{p.suffix or '.jsonl'}")
        if p.parent and str(p.parent) not in ("", "."):
            parent = p.parent
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                return None
        return str(unique) if unique else None

    # ---- recording (sync, non-blocking, never raises) ----

    def record(self, stage: str, **fields):
        try:
            if stage not in TRACE_STAGES:
                return  # unknown stage: refuse, never log
            clean, _dropped = _sanitize_fields(stage, fields)
            rec = {"conn_id": self.conn_id,
                   "t_rel": round(time.monotonic() - self.t0, 4),
                   "kind": stage}
            rec.update(clean)
            with self._lock:
                if len(self._records) >= self.max_records:
                    self.evicted += 1
                self._records.append(rec)
                self._seq += 1
        except Exception:  # noqa: BLE001 - tracing must never affect the session
            pass

    def snapshot(self) -> list:
        with self._lock:
            return list(self._records)

    def close(self):
        """Final flush then stop the writer thread. Safe against a concurrent
        writer flush (RLock + final flush after join)."""
        self._stopped.set()
        if getattr(self, "_writer", None) is not None \
                and self._writer is not threading.current_thread():
            try:
                self._writer.join(2.0)
            except Exception:  # noqa: BLE001
                pass
        self.flush_now()  # final flush after the writer is gone

    # ---- bounded per-connection file writer ----

    def _write_loop(self):
        while not self._stopped.is_set():
            self._stopped.wait(self.flush_interval_s)
            self.flush_now()
        self.flush_now()

    def flush_now(self):
        if not self.file_path:
            return
        with self._lock:
            recs = list(self._records)
            evicted = self.evicted
            seq = self._seq
        if seq == self._unwritten_seq:
            return  # nothing new since the last mutation
        tmp = self.file_path + ".tmp"  # unique per connection, single writer
        with self._write_lock:
            if self._unwritten_seq == seq:
                return  # another flush already handled this seq
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(json.dumps({"trace": "fscribe-lifecycle",
                                        "conn_id": self.conn_id,
                                        "records_n": len(recs),
                                        "evicted_by_bound": evicted,
                                        "write_failures": self.write_failures,
                                        "seq": seq},
                                       ensure_ascii=False))
                    f.write("\n")
                    for r in recs:
                        f.write(json.dumps(r, ensure_ascii=False))
                        f.write("\n")
                os.replace(tmp, self.file_path)  # atomic bounded overwrite
                self._unwritten_seq = seq
            except Exception:  # noqa: BLE001 - trace flush must never break a session
                self.write_failures += 1


def new_session_tracer() -> "SessionTracer | None":
    """App-level opt-in factory: one tracer (and one unique file) per WS
    connection. Returns None (zero overhead, nothing written) unless
    FSCRIBE_LIFECYCLE_TRACE=1."""
    if (os.environ.get("FSCRIBE_LIFECYCLE_TRACE") or "").strip() != "1":
        return None
    max_records = None
    raw = (os.environ.get("FSCRIBE_LIFECYCLE_TRACE_MAX") or "").strip()
    if raw.isdigit():
        max_records = int(raw)
    raw = (os.environ.get("FSCRIBE_LIFECYCLE_TRACE_FLUSH_S") or "").strip()
    flush_s = None
    try:
        flush_s = float(raw) if raw else None
    except ValueError:
        flush_s = None
    path = os.environ.get("FSCRIBE_LIFECYCLE_TRACE_FILE") or None
    try:
        return SessionTracer(file_dir_or_path=path, max_records=max_records,
                             flush_interval_s=flush_s)
    except Exception:  # noqa: BLE001
        return None
