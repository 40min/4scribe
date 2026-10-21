"""Mobile batch transcription endpoint for HardAfanasii (ordinary HTTP multipart).

Route: POST /4scribe/v1/audio/transcriptions (+ an unprefixed loopback alias,
mirroring the existing health/WS route pattern). Contract: docs/mobile-batch-api.md.

Request
  Authorization: Bearer <FSCRIBE_MOBILE_TOKEN>   (SEPARATE from the WS token)
  multipart/form-data, exactly one `file` part: M4A/AAC or WAV, <= 2 MiB,
  decoded duration <= 61 s. Optional `language` = ru | en; omitted = auto
  (no assistant prefill, exactly like the current Android omission).

Response (all numeric fields are finite, non-negative, never null)
  text, enhanced_text=null, enhancer_error=false, language, audio_duration_ms,
  conversion_ms, stt_ms, enhancer_ms=0, server_total_ms, rtf

Design guards (see docs/mobile-batch-api.md for the owner-facing summary):
- Separate mobile Bearer, constant-time compare, validated before any body
  parsing. Missing/placeholder/short/WS-equal token disables the endpoint
  (503 fail closed), it never falls back to FSCRIBE_TOKEN or a test default.
- Content-Length is advisory only. The body is consumed through a hard byte
  cap, so a chunked upload cannot bypass the 2 MiB file limit; the file part
  is capped again after parsing.
- One active batch request: a second concurrent upload gets 503 and is never
  queued. The shared ASR executor and the shared decode semaphore are reused
  for ffmpeg decode and ASR, so batch work can never exceed one inference slot
  next to live WSS traffic, and the Silero verifier stays on its single-worker
  pool.
- ffmpeg is a CPU subprocess on a private temp file (0600, dir 0700) with a
  bounded wall clock and bounded stdout; it is killed and reaped on
  timeout/cancel. Duration is measured on the DECODED PCM, never metadata.
- The 61 s client limit is preserved without truncation: PCM is split at the
  lowest-energy point into windows <= FSCRIBE_MOBILE_ASR_WINDOW_S (40 s
  default) and stitched verbatim. No LLM rewrite, no invented word dedup.
- No enhancer: `enhanced_text` is null, `enhancer_error` false, `enhancer_ms`
  numeric 0. An old `enhance` flag is accepted and ignored; no LLM call.
- Logs carry status/sizes/timings only - never audio, transcripts or tokens.
"""
import asyncio
import hmac
import logging
import os
import selectors
import shutil
import subprocess
import tempfile
import threading
import time

from fastapi import Request
from fastapi.responses import JSONResponse

try:  # starlette >= 0.21
    from starlette.requests import ClientDisconnect
except Exception:  # pragma: no cover - defensive import only
    class ClientDisconnect(Exception):
        pass

try:  # required by starlette's multipart form parser (pinned dependency)
    import python_multipart  # noqa: F401
    _MULTIPART_AVAILABLE = True
except Exception:  # pragma: no cover - reported as a disabled-batch reason
    _MULTIPART_AVAILABLE = False

logger = logging.getLogger("fscribe.batch")

SAMPLE_RATE = 16000
#: Hard client contract: Android caps the recording at 2 MiB / 61 000 ms.
MAX_FILE_BYTES = 2 * 1024 * 1024
#: Explicit multipart envelope allowance (boundaries + part headers only).
MULTIPART_OVERHEAD_BYTES = 64 * 1024
MAX_BODY_BYTES = MAX_FILE_BYTES + MULTIPART_OVERHEAD_BYTES
#: Container priming/rounding allowance at the 61 000 ms client limit only.
DECODED_DURATION_TOLERANCE_S = 0.1
MIN_MOBILE_TOKEN_LEN = 16
PLACEHOLDER_TOKENS = frozenset({"test-token", "changeme", "change-me"})

#: Loopback alias + tailnet-prefixed route (Serve proxies the path verbatim,
#: exactly like REALTIME_WS_PATHS, so the app answers under the prefix).
BATCH_HTTP_PATHS = (
    "/4scribe/v1/audio/transcriptions",
    "/v1/audio/transcriptions",
)

#: Declared content type -> ffmpeg input demuxer (explicit allowlist).
AUDIO_CONTENT_TYPES = {
    "audio/mp4": "mov",
    "audio/m4a": "mov",
    "audio/x-m4a": "mov",
    "audio/aac": "aac",
    "audio/aacp": "aac",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/vnd.wave": "wav",
}
#: Extension fallback only for `application/octet-stream` / missing type.
AUDIO_EXTENSIONS = {".m4a": "mov", ".mp4": "mov", ".aac": "aac", ".wav": "wav"}


class BatchError(Exception):
    """Public, secret-free error: status + stable code + generic message."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message

    def response(self) -> JSONResponse:
        return JSONResponse(status_code=self.status,
                            content={"error": self.code, "message": self.message})


class AudioDecodeError(Exception):
    """Input is not decodable audio in the declared allowlisted container."""


class AudioTooLongError(Exception):
    """Decoded PCM exceeded the bounded output cap (duration bomb)."""


class AudioConversionTimeout(Exception):
    """The ffmpeg subprocess exceeded its bounded wall clock."""


class AudioConversionCancelled(Exception):
    """The caller cancelled; the subprocess was killed before reaping."""


def _error(status: int, code: str, message: str) -> JSONResponse:
    return BatchError(status, code, message).response()


# ---------------------------------------------------------------- auth ------

def _resolve_mobile_token(cfg):
    """Return (token, reason). Empty token means batch is disabled (503).

    The mobile Bearer must be its own secret: reusing the realtime token,
    a placeholder, or a trivially short value would turn a client-key leak
    into a WSS compromise, so those configurations fail closed.
    """
    token = (getattr(cfg, "mobile_token", "") or "").strip()
    if not token:
        return "", "unset"
    if token.lower() in PLACEHOLDER_TOKENS:
        return "", "placeholder"
    ws_token = (getattr(cfg, "token", "") or "").strip()
    if ws_token and hmac.compare_digest(token.encode("utf-8"), ws_token.encode("utf-8")):
        return "", "shared_with_realtime_token"
    if len(token) < MIN_MOBILE_TOKEN_LEN:
        return "", "too_short"
    return token, "ok"


def authorized(header_value, token) -> bool:
    """Constant-time Bearer check. Missing/blank tokens always fail."""
    if not token or not header_value:
        return False
    scheme, _, value = header_value.partition(" ")
    if scheme.lower() != "bearer":
        return False
    value = value.strip()
    if not value:
        return False
    return hmac.compare_digest(value.encode("utf-8"), token.encode("utf-8"))


# ------------------------------------------------------- bounded upload -----

async def read_bounded_body(request, limit: int = MAX_BODY_BYTES):
    """Consume the request body through a hard byte cap.

    Content-Length is only an early hint: it can be absent (chunked) or lie,
    so the authoritative check is the running total while streaming. Returns
    None when the cap is exceeded (caller answers 413) after having read at
    most `limit` bytes.
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            length = int(declared)
        except (TypeError, ValueError):
            raise BatchError(400, "invalid_request", "invalid content-length")
        if length < 0:
            raise BatchError(400, "invalid_request", "invalid content-length")
        if length > limit:
            return None
    body = bytearray()
    try:
        async for chunk in request.stream():
            if len(body) + len(chunk) > limit:
                return None
            body.extend(chunk)
    except ClientDisconnect:
        raise BatchError(400, "invalid_request", "incomplete upload")
    if not body:
        raise BatchError(400, "invalid_request", "empty request body")
    return bytes(body)


def _resolve_container(content_type, filename):
    """Explicit M4A/AAC/WAV allowlist -> ffmpeg demuxer, else None."""
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    if ctype in AUDIO_CONTENT_TYPES:
        return AUDIO_CONTENT_TYPES[ctype]
    if ctype in ("", "application/octet-stream"):
        return AUDIO_EXTENSIONS.get(os.path.splitext(filename or "")[1].lower())
    return None


def _resolve_language(value):
    """Omitted/auto -> None (no prefill). Explicit ru/en validated."""
    raw = value.strip().lower() if isinstance(value, str) else ""
    if raw in ("", "auto"):
        return None
    if raw in ("ru", "en"):
        return raw
    raise BatchError(400, "invalid_language", "language must be ru, en or omitted")


def _form_text(form, name, fallback=None):
    """Read one optional text field; ignore non-text parts and old flags."""
    try:
        value = form.get(name)
    except AttributeError:  # pragma: no cover - non-FormData fallback
        value = None
    if isinstance(value, str):
        return value
    return fallback


# ------------------------------------------------------------ ffmpeg path ---

def build_ffmpeg_argv(binary, container, path):
    """Explicit demuxer + mono 16 kHz PCM16 on stdout; no metadata probing."""
    return [
        binary, "-nostdin", "-hide_banner", "-loglevel", "error", "-threads", "1",
        "-f", container, "-i", path,
        "-vn", "-sn", "-dn", "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1",
    ]


def run_bounded_command(argv, max_bytes, timeout_s, cancel_event=None) -> bytes:
    """Run one subprocess with a bounded wall clock and bounded stdout.

    Never buffers more than max_bytes (+ one read chunk) of output, kills and
    reaps the child on timeout/cancel/overflow, and reports classified errors.
    stderr is discarded so a chatty decoder can never fill a pipe or a log.
    """
    deadline = time.monotonic() + max(float(timeout_s), 0.001)
    proc = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            close_fds=True)
    out = bytearray()
    timed_out = cancelled = too_long = False
    selector = None
    try:
        fd = proc.stdout.fileno()
        os.set_blocking(fd, False)
        selector = selectors.DefaultSelector()
        selector.register(fd, selectors.EVENT_READ)
        while True:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            selector.select(min(remaining, 0.1))
            try:
                chunk = os.read(fd, 262144)
            except BlockingIOError:
                continue
            except OSError:
                break
            if not chunk:
                break
            out.extend(chunk)
            if len(out) > max_bytes:
                too_long = True
                break
    finally:
        if selector is not None:
            try:
                selector.close()
            except Exception:  # noqa: BLE001 - fd already gone
                pass
        try:
            proc.stdout.close()
        except Exception:  # noqa: BLE001
            pass
        if proc.poll() is None and (timed_out or cancelled or too_long):
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - unkillable child
                pass
    if cancelled:
        raise AudioConversionCancelled()
    if timed_out:
        raise AudioConversionTimeout()
    if too_long:
        raise AudioTooLongError()
    if proc.returncode != 0:
        raise AudioDecodeError("decoder exited %s" % proc.returncode)
    if not out:
        raise AudioDecodeError("no decoded audio")
    if len(out) % 2:
        del out[-1:]
    return bytes(out)


def decode_with_ffmpeg(path, container, max_bytes, timeout_s, cancel_event=None,
                       binary="ffmpeg") -> bytes:
    """Decode an allowlisted upload to PCM16 mono 16 kHz, bounded end to end."""
    return run_bounded_command(build_ffmpeg_argv(binary, container, path),
                               max_bytes, timeout_s, cancel_event)


# --------------------------------------------------------------- chunking ---

def _frame_energy(samples, start, length):
    end = min(start + length, len(samples))
    total = 0
    for i in range(start, end):
        value = samples[i]
        total += value * value
    return total


def plan_chunks(pcm, max_window_s, rate=SAMPLE_RATE, frame_ms=20,
                search_s=1.5, min_chunk_s=0.25):
    """Split PCM into contiguous windows of at most max_window_s seconds.

    The split point is the lowest-energy point (nearest pause) within
    `search_s` before the nominal boundary. Windows tile the WHOLE buffer
    (no overlap, no dropped tail) so 61 s audio is never truncated.
    """
    total = len(pcm) // 2
    max_samples = max(int(max_window_s * rate), 1)
    if total <= max_samples:
        return [(0, total)]
    samples = memoryview(pcm)[: total * 2].cast("h")
    frame = max(int(frame_ms * rate / 1000), 1)
    search = max(int(search_s * rate), 0)
    min_chunk = max(int(min_chunk_s * rate), 1)
    chunks = []
    start = 0
    while total - start > max_samples:
        nominal = start + max_samples
        lo = max(start + min_chunk, nominal - search)
        best_energy = None
        split = nominal
        pos = lo
        while pos <= nominal:
            energy = _frame_energy(samples, pos, min(frame, total - pos))
            if best_energy is None or energy < best_energy:
                best_energy = energy
                split = pos
            pos += frame
        if split <= start:
            split = nominal
        chunks.append((start, split))
        start = split
    chunks.append((start, total))
    return chunks


def stitch_texts(texts):
    """Verbatim stitch: whitespace-normalised join, no rewrite or dedup."""
    return " ".join(part.strip() for part in texts if part and part.strip())


def close_form(form) -> None:
    """Synchronously release every parsed multipart part's resources.

    Deliberately sync: cleanup has to run on deadline/cancel paths without
    another await point. `UploadFile.close()` is only async because it wraps
    `SpooledTemporaryFile.close()` in a threadpool; closing the underlying
    file here is equivalent and cannot leak a spool on any path (success,
    413/400 validation, malformed form, cancellation).
    """
    if form is None:
        return
    try:
        items = list(form.multi_items())
    except Exception:  # noqa: BLE001 - non-FormData fallback
        items = []
    for _key, value in items:
        if isinstance(value, str):
            continue
        inner = getattr(value, "file", None)
        closer = getattr(inner, "close", None)
        if closer is None:
            continue
        try:
            closer()
        except Exception:  # noqa: BLE001 - already closed/aborted
            pass



# ------------------------------------------------------------ transcriber ---

async def _drain_future(fut):
    """Wait for already-submitted native work; never release early.

    A cancelled or timed-out HTTP request must not free the inference slot
    while a thread (ffmpeg / ASR) is still running. Repeated cancellation is
    absorbed until the underlying work really ends.
    """
    while not fut.done():
        try:
            await asyncio.wait({fut}, timeout=0.5)
        except asyncio.CancelledError:
            continue
    if not fut.cancelled():
        try:
            fut.exception()
        except Exception:  # noqa: BLE001 - result is discarded anyway
            pass


class BatchTranscriber:
    """One-shot multipart transcription over the app's shared inference seams."""

    def __init__(self, cfg, *, asr, pool, decode_sem, verifier, vad_pool=None):
        self.cfg = cfg
        self.asr = asr
        self.pool = pool
        self.decode_sem = decode_sem
        self.verifier = verifier
        self.vad_pool = vad_pool
        self.total_timeout_s = max(float(getattr(cfg, "mobile_total_timeout_s", 90.0)), 1.0)
        self.convert_timeout_s = max(float(getattr(cfg, "mobile_convert_timeout_s", 15.0)), 0.1)
        self.max_duration_s = max(float(getattr(cfg, "mobile_max_duration_s", 61.0)), 1.0)
        window = float(getattr(cfg, "mobile_asr_window_s", 40.0))
        self.asr_window_s = min(max(window, 5.0), 60.0)
        self.ffmpeg_bin = getattr(cfg, "mobile_ffmpeg_bin", "ffmpeg") or "ffmpeg"
        self.token, self.disabled_reason = _resolve_mobile_token(cfg)
        if self.token and not _MULTIPART_AVAILABLE:
            # Fail closed with a precise reason instead of answering every
            # upload with a misleading 400: the image missing python-multipart
            # is an operator/deployment fault, not a client fault.
            self.token, self.disabled_reason = "", "multipart_unavailable"
        #: decoded sample cap: client 61 s limit + narrow encoder tolerance
        self.max_samples = int((self.max_duration_s + DECODED_DURATION_TOLERANCE_S) * SAMPLE_RATE)
        self._active = asyncio.Lock()
        if self.enabled:
            logger.info("mobile batch API enabled paths=%s window_s=%s",
                        ",".join(BATCH_HTTP_PATHS), self.asr_window_s)
        else:
            logger.warning("mobile batch API disabled: %s", self.disabled_reason)

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def status(self):
        return {"enabled": self.enabled, "reason": self.disabled_reason}

    # ------------------------------------------------------------- route ----

    async def handle(self, request: Request):
        if not self.enabled:
            return _error(503, "batch_unconfigured",
                          "mobile batch transcription is not configured")
        if not authorized(request.headers.get("authorization"), self.token):
            return _error(401, "unauthorized", "missing or invalid bearer token")
        if self._active.locked():
            return _error(503, "busy", "another batch transcription is in progress")
        await self._active.acquire()
        started = time.monotonic()
        deadline = started + self.total_timeout_s
        cancel_event = threading.Event()
        tmpdir = None
        form = None
        try:
            body = await self._await_bounded(read_bounded_body(request), deadline)
            if body is None:
                raise BatchError(413, "payload_too_large",
                                 "upload exceeds the 2 MiB recording limit")
            form, upload = await self._extract_upload(request, body, deadline)
            language = _resolve_language(_form_text(form, "language",
                                                    request.query_params.get("language")))
            content_type = upload.content_type
            filename = upload.filename
            data = await upload.read(MAX_FILE_BYTES + 1)
            # Release every parsed part's spool before any further work.
            close_form(form)
            form = None
            if len(data) > MAX_FILE_BYTES:
                raise BatchError(413, "payload_too_large",
                                 "upload exceeds the 2 MiB recording limit")
            if not data:
                raise BatchError(400, "invalid_audio", "empty audio part")
            container = _resolve_container(content_type, filename)
            if container is None:
                raise BatchError(400, "unsupported_audio_format",
                                 "audio must be M4A/AAC or WAV")
            tmpdir, path = self._spool(data)
            async with self._semaphore_slot(deadline):
                pcm, conversion_s = await self._convert(path, container, deadline,
                                                        cancel_event)
                samples = len(pcm) // 2
                if samples <= 0:
                    raise BatchError(400, "invalid_audio", "no audio decoded")
                if samples > self.max_samples:
                    raise BatchError(413, "audio_too_long",
                                     "decoded audio exceeds the 61 s limit")
                if not await self._has_speech(pcm, deadline):
                    return self._success("", language, samples, conversion_s, 0.0,
                                         started, mode="verifier_declined")
                texts, stt_s = await self._transcribe(pcm, language, deadline)
            text = stitch_texts(texts)
            return self._success(text, language, samples, conversion_s, stt_s, started)
        except BatchError as exc:
            logger.info("batch transcription rejected: status=%d code=%s",
                        exc.status, exc.code)
            return exc.response()
        except asyncio.CancelledError:
            # Stop ffmpeg promptly; submitted native work is drained by the
            # helpers above before this handler unwinds and frees its slot.
            cancel_event.set()
            raise
        except Exception as exc:  # noqa: BLE001 - never leak internals
            logger.warning("batch transcription failed: kind=%s", type(exc).__name__)
            return _error(500, "internal_error", "transcription failed")
        finally:
            close_form(form)  # no-op after the normal-path close above
            if tmpdir is not None:
                shutil.rmtree(tmpdir, ignore_errors=True)
            self._active.release()

    # ---------------------------------------------------------- internals ---

    async def _await_bounded(self, awaitable, deadline):
        """Await client-facing I/O bounded by the remaining total budget.

        Covers the upload/body read and multipart form parsing so a slow
        authenticated upload cannot hold the single batch admission slot past
        the deadline. Nothing is submitted to a worker thread here, so
        cancelling the coroutine is sufficient cleanup.
        """
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # The coroutine was created before this check: close it so an
            # already-expired budget cannot leak a "never awaited" object.
            closer = getattr(awaitable, "close", None)
            if closer is not None:
                closer()
            raise BatchError(504, "deadline_exceeded",
                             "transcription deadline exceeded")
        try:
            return await asyncio.wait_for(awaitable, remaining)
        except TimeoutError:
            raise BatchError(504, "deadline_exceeded",
                             "transcription deadline exceeded")

    async def _extract_upload(self, request, body, deadline):
        """Replay the bounded body into starlette's multipart parser.

        Every parsed part is released on a validation failure too, not only
        the selected `file` upload.
        """
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        scope = dict(request.scope)
        headers = [(k, v) for k, v in (scope.get("headers") or [])
                   if k.lower() != b"content-length"]
        headers.append((b"content-length", str(len(body)).encode("ascii")))
        scope["headers"] = headers
        replay = Request(scope, receive)
        try:
            parse = replay.form(max_files=2, max_fields=8,
                                max_part_size=MAX_FILE_BYTES + 4096)
        except TypeError:  # older starlette without limit kwargs
            parse = replay.form()
        try:
            form = await self._await_bounded(parse, deadline)
        except BatchError:
            raise
        except Exception:  # noqa: BLE001 - malformed multipart is a client error
            raise BatchError(400, "invalid_request", "malformed multipart body")
        try:
            try:
                files = form.getlist("file")
            except AttributeError:  # pragma: no cover - non-FormData fallback
                files = [form.get("file")]
            if len(files) != 1:
                raise BatchError(400, "invalid_request",
                                 "exactly one 'file' part is required")
            upload = files[0]
            if isinstance(upload, str) or not hasattr(upload, "read"):
                raise BatchError(400, "invalid_request",
                                 "'file' part must be a file")
            return form, upload
        except BaseException:
            close_form(form)
            raise

    def _spool(self, data):
        """Private, mode-0600 temp file; removed by the handler's finally."""
        tmpdir = tempfile.mkdtemp(prefix="fscribe-batch-")
        os.chmod(tmpdir, 0o700)
        path = os.path.join(tmpdir, "upload.bin")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        return tmpdir, path

    def _semaphore_slot(self, deadline):
        transcriber = self

        class _Slot:
            async def __aenter__(self):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BatchError(504, "deadline_exceeded",
                                     "transcription deadline exceeded")
                try:
                    await asyncio.wait_for(transcriber.decode_sem.acquire(), remaining)
                except TimeoutError:
                    raise BatchError(504, "deadline_exceeded",
                                     "transcription deadline exceeded")
                return None

            async def __aexit__(self, *exc_info):
                transcriber.decode_sem.release()
                return False

        return _Slot()

    async def _convert(self, path, container, deadline, cancel_event):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BatchError(504, "deadline_exceeded",
                             "transcription deadline exceeded")
        timeout_s = min(self.convert_timeout_s, remaining)
        max_bytes = self.max_samples * 2
        fn = (lambda: decode_with_ffmpeg(path, container, max_bytes, timeout_s,
                                         cancel_event, self.ffmpeg_bin))
        started = time.monotonic()
        try:
            pcm = await self._run_in_pool(fn, deadline, cancel_event)
        except AudioTooLongError:
            raise BatchError(413, "audio_too_long",
                             "decoded audio exceeds the 61 s limit")
        except AudioConversionTimeout:
            raise BatchError(504, "deadline_exceeded", "audio conversion timed out")
        except AudioConversionCancelled:
            raise BatchError(504, "deadline_exceeded", "transcription cancelled")
        except AudioDecodeError:
            raise BatchError(400, "invalid_audio", "audio could not be decoded")
        except TimeoutError:
            raise BatchError(504, "deadline_exceeded", "audio conversion timed out")
        return pcm, time.monotonic() - started

    async def _run_in_pool(self, fn, deadline, cancel_event=None):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(self.pool, fn)
        try:
            return await asyncio.wait_for(asyncio.shield(fut), remaining)
        except (asyncio.CancelledError, TimeoutError):
            # Signal the decoder to kill its child promptly, then keep the
            # admission slot until the worker has really returned.
            if cancel_event is not None:
                cancel_event.set()
            await _drain_future(fut)
            raise

    async def _has_speech(self, pcm, deadline):
        """Silero verification on the shared single-worker pool.

        The verifier thread cannot be cancelled, so its future is shielded and
        drained before the admission slot is released - exactly like ASR. A
        fault fails OPEN (never delete real speech).
        """
        verifier = self.verifier
        if verifier is None or not getattr(verifier, "enabled", False):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BatchError(504, "deadline_exceeded",
                             "transcription deadline exceeded")
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(self.vad_pool, verifier.verify, pcm)
        try:
            return bool(await asyncio.wait_for(asyncio.shield(fut), remaining))
        except TimeoutError:
            # Capacity is retained until the native verification really ends.
            await _drain_future(fut)
            raise BatchError(504, "deadline_exceeded",
                             "transcription deadline exceeded")
        except asyncio.CancelledError:
            await _drain_future(fut)
            raise
        except Exception as exc:  # noqa: BLE001 - fail OPEN, never delete speech
            logger.warning("batch verifier fault (%s); accepting audio",
                           type(exc).__name__)
            return True

    async def _transcribe(self, pcm, language, deadline):
        texts = []
        stt_s = 0.0
        for begin, end in plan_chunks(pcm, self.asr_window_s):
            segment = pcm[begin * 2:end * 2]
            if not segment:
                continue
            started = time.monotonic()
            try:
                result = await self._decode(segment, language, deadline)
            except TimeoutError:
                raise BatchError(504, "deadline_exceeded",
                                 "transcription deadline exceeded")
            stt_s += time.monotonic() - started
            text = getattr(result, "text", "") or ""
            if text.strip():
                texts.append(text)
        return texts, stt_s

    async def _decode(self, pcm, language, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(self.pool, self.asr.decode, pcm, language)
        try:
            return await asyncio.wait_for(asyncio.shield(fut), remaining)
        except TimeoutError:
            # The native Ollama call cannot be cancelled (its own HTTP timeout
            # is 600 s): keep the admission slot until it really ends, then
            # report the deadline. New uploads are refused meanwhile (503), so
            # work never stacks on top of in-flight inference.
            await _drain_future(fut)
            raise
        except asyncio.CancelledError:
            await _drain_future(fut)
            raise
        except Exception as exc:  # noqa: BLE001 - classified below
            raise BatchError(502, "asr_unavailable",
                             "speech recognition is unavailable") from exc

    def _success(self, text, language, samples, conversion_s, stt_s, started,
                 mode="ok"):
        audio_s = samples / SAMPLE_RATE
        total_s = time.monotonic() - started
        payload = {
            "text": text,
            "enhanced_text": None,
            "enhancer_error": False,
            "language": language or "auto",
            "audio_duration_ms": int(round(audio_s * 1000)),
            "conversion_ms": int(round(conversion_s * 1000)),
            "stt_ms": int(round(stt_s * 1000)),
            "enhancer_ms": 0,
            "server_total_ms": int(round(total_s * 1000)),
            "rtf": round(stt_s / audio_s, 3) if audio_s > 0 else 0.0,
        }
        logger.info("batch transcription ok: mode=%s audio_ms=%d conversion_ms=%d "
                    "stt_ms=%d total_ms=%d",
                    mode, payload["audio_duration_ms"], payload["conversion_ms"],
                    payload["stt_ms"], payload["server_total_ms"])
        return JSONResponse(status_code=200, content=payload)
