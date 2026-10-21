"""Standalone text correction endpoint for Loqui.

Public route: POST /4scribe/v1/text/enhance. The ingress strips `/4scribe`, so
the same authenticated handler/admission is also registered as
POST /v1/text/enhance (matching batch/realtime).

This is a behaviour-preserving port of the existing parakeet-test text
enhancer. The corrector prompt, Ollama options, response guards and public
error codes are intentionally unchanged; prompt/model quality work is a
separate later task.

Request
  Authorization: Bearer <FSCRIBE_ENHANCEMENT_TOKEN>  (its own key)
  application/json: {"text": "...", "language": "auto" | "ru" | "en"}

Response (Loqui-compatible success contract plus additive fields)
  {"status": "unchanged" | "corrected", "text": <display string>,
   "raw_text": <exact original>, "enhanced_text": <corrected> | null}

Guards (see docs/enhancement-api.md):
- authentication happens before any body bytes are read;
- one active enhancement request, no queue (429 busy);
- one 5 s total deadline covers slow upload/body and the upstream call;
- request body is capped while streaming at 32 KiB (Content-Length is only a
  hint) and text at 4000 characters, rejected rather than truncated;
- upstream response is capped while streaming at 512 KiB;
- the 5 s client deadline and the finite 60 s wall-clock upstream call budget
  are separate; a 504 keeps admission until the submitted call really
  completes;
- a timeout/cancel does NOT cancel native Ollama generation. If transport
  loss leaves native completion unprovable, the endpoint latches unavailable
  (health `completion_unknown`) until an explicit service restart;
- logs carry status/sizes only, never text or tokens.
"""
import asyncio
import hmac
import inspect
import json
import logging
import time

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse

try:  # starlette >= 0.21
    from starlette.requests import ClientDisconnect
except Exception:  # pragma: no cover - defensive import only
    class ClientDisconnect(Exception):
        pass

logger = logging.getLogger("fscribe.enhancement")

#: Public prefixed route plus the unprefixed alias the ingress calls after it
#: strips `/4scribe` (same pattern as the batch and realtime routes).
TEXT_ENHANCE_PATHS = (
    "/4scribe/v1/text/enhance",
    "/v1/text/enhance",
)

ENHANCER_MODEL_DEFAULT = "qwen3:0.6b"
ENHANCER_SYSTEM_PROMPT = (
    "You are an ASR transcript corrector. Correct the transcript while "
    "preserving exactly what the speaker meant. Never drop or add words.\n"
    "Rules:\n"
    "1. Preserve the original language: Russian speech stays Russian. Never "
    "translate.\n"
    "2. Fix punctuation and capitalization.\n"
    "3. Keep every word of the input in the same order unless it is an "
    "obvious speech-recognition error.\n"
    "4. Inside a Russian sentence, when a word or phrase is a well-known "
    "product, brand, or technical term written phonetically in Cyrillic, "
    "always write it in its canonical English spelling with exact "
    "capitalization: докер→Docker, компоуз→Compose, контейнер→container "
    "only if part of a product phrase, тайлскейл→Tailscale, "
    "хоум ассистент→Home Assistant, апи→API, веб юай→Web UI, конфиг→config."
    "\n"
    "5. Replace spelled-out Russian number words with digits "
    "(три тысячи→3000, пять→5) when highly confident.\n"
    "6. If the ENTIRE input is clearly an English sentence phonetically "
    "transcribed in Cyrillic letters, rewrite it in proper English.\n"
    "7. Do not paraphrase, do not answer, do not explain. Keep the wording "
    "otherwise identical to the input.\n"
    "8. Return only the corrected transcript.\n\n"
    "Examples:\n"
    "Input: запусти докер компоуз и проверь тайлскейл\n"
    "Output: Запусти Docker Compose и проверь Tailscale.\n"
    "Input: ай вонт ту чек зе докер контейнер\n"
    "Output: I want to check the Docker container.\n"
    "Input: какая сегодня погода в эспоо\n"
    "Output: Какая сегодня погода в Эспоо?"
)

BODY_LIMIT = 32 * 1024
MAX_TEXT_CHARS = 4000
MAX_RESPONSE_BYTES = 512 * 1024
DEADLINE_S = 5.0
UPSTREAM_TIMEOUT_S = 60.0
SHUTDOWN_WAIT_S = 1.0
MIN_TOKEN_LEN = 16
PLACEHOLDER_TOKENS = frozenset({"test-token", "changeme", "change-me"})
LANGUAGES = ("auto", "ru", "en")

ERROR_STATUS = {
    "unauthorized": 401,
    "invalid_input": 422,
    "input_too_large": 413,
    "enhancement_busy": 429,
    "enhancement_unavailable": 503,
    "invalid_model_output": 502,
    "enhancement_timeout": 504,
}


class EnhanceError(Exception):
    """Corrector failure carrying the public endpoint error code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code
        self.status = ERROR_STATUS.get(code, 503)

    def response(self) -> JSONResponse:
        return JSONResponse({"error": {"code": self.code}}, status_code=self.status)


class BodyTooLarge(Exception):
    """The raw JSON body exceeded the streaming cap."""


class UpstreamCompletionUnknown(Exception):
    """The HTTP transport ended without proving that generation completed.

    A read/write timeout or connection loss after submission means Ollama may
    still be generating. There is no safe way to infer native completion from
    an HTTP disconnect, so the endpoint latches unavailable (see docs).
    """


def _error(code: str) -> JSONResponse:
    return EnhanceError(code).response()


def _consume_task_exception(task):
    """Mark a task's result/exception retrieved (bounded cleanup helper)."""
    if not task.done():
        return
    try:
        task.exception()
    except BaseException:  # noqa: BLE001 - cancellation is expected
        pass


def _enhance_options(text_len: int) -> dict:
    """Ollama sampling options, unchanged from the existing corrector.

    num_ctx is constant: Ollama reloads the model KV cache whenever the
    requested context changes, so scaling it per input would thrash.
    """
    num_predict = 120 if text_len <= 200 else min(1024, (text_len * 4) // 5)
    return {"temperature": 0, "num_predict": num_predict, "num_ctx": 4096}


def _resolve_enhancement_token(cfg):
    """Return (token, reason); empty token disables the endpoint (503).

    The enhancement Bearer must be its own secret. Reusing the realtime or
    mobile key, a placeholder, or a trivially short value would widen a
    client-key leak, so those configurations fail closed.
    """
    token = (getattr(cfg, "enhancement_token", "") or "").strip()
    if not token:
        return "", "unset"
    if token.lower() in PLACEHOLDER_TOKENS:
        return "", "placeholder"
    ws_token = (getattr(cfg, "token", "") or "").strip()
    if ws_token and hmac.compare_digest(token.encode("utf-8"), ws_token.encode("utf-8")):
        return "", "shared_with_realtime_token"
    mobile_token = (getattr(cfg, "mobile_token", "") or "").strip()
    if mobile_token and hmac.compare_digest(token.encode("utf-8"),
                                            mobile_token.encode("utf-8")):
        return "", "shared_with_mobile_token"
    if len(token) < MIN_TOKEN_LEN:
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


async def read_bounded_body(request, limit: int = BODY_LIMIT) -> bytes:
    """Consume the body through a hard chunk-stream cap.

    Content-Length is advisory: it may be absent (chunked) or understated.
    The authoritative check is the running total, so an oversized body is
    rejected without ever being truncated and parsed as valid JSON.
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            length = int(declared)
        except (TypeError, ValueError):
            length = None
        if length is not None and length > limit:
            raise BodyTooLarge()
    body = bytearray()
    try:
        async for chunk in request.stream():
            if len(body) + len(chunk) > limit:
                raise BodyTooLarge()
            body.extend(chunk)
    except ClientDisconnect:
        raise EnhanceError("invalid_input")
    return bytes(body)


class TextEnhancer:
    """One bounded, authenticated corrector slot for the app's lifecycle."""

    def __init__(self, cfg, client_factory=None):
        self.cfg = cfg
        self.model = (getattr(cfg, "enhancer_model", "") or ENHANCER_MODEL_DEFAULT).strip()
        deadline = getattr(cfg, "enhancement_deadline_s", DEADLINE_S)
        try:
            self.deadline_s = max(float(deadline), 0.05)
        except (TypeError, ValueError):
            self.deadline_s = DEADLINE_S
        upstream_timeout = getattr(cfg, "enhancement_upstream_timeout_s",
                                   UPSTREAM_TIMEOUT_S)
        try:
            self.upstream_timeout_s = max(float(upstream_timeout), 0.05)
        except (TypeError, ValueError):
            self.upstream_timeout_s = UPSTREAM_TIMEOUT_S
        self.max_text_chars = MAX_TEXT_CHARS
        self.body_limit = BODY_LIMIT
        self.max_response_bytes = MAX_RESPONSE_BYTES
        self.token, self.disabled_reason = _resolve_enhancement_token(cfg)
        self._slot = asyncio.Semaphore(1)
        # At most one submitted work item exists because the slot stays held
        # until it finishes; keep the release task strongly referenced.
        self._retained = set()
        self._work_tasks = set()
        self._completion_unknown = False
        self._closing = False
        self._client = None
        self._client_factory = client_factory or (
            lambda: httpx.AsyncClient(base_url=cfg.ollama_url,
                                      timeout=httpx.Timeout(self.deadline_s)))
        if self.enabled:
            logger.info("text enhancement API enabled path=%s model=%s",
                        ",".join(TEXT_ENHANCE_PATHS), self.model)
        else:
            logger.warning("text enhancement API disabled: %s", self.disabled_reason)

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def status(self):
        return {"enabled": self.enabled, "reason": self.disabled_reason,
                "completion_unknown": self._completion_unknown}

    async def start(self):
        """Create the async HTTP client on app startup (never module-level)."""
        if self._closing:
            raise EnhanceError("enhancement_unavailable")
        if self._client is None:
            client = self._client_factory()
            if inspect.isawaitable(client):
                client = await client
            self._client = client

    async def close(self):
        """Bounded shutdown: cancel in-flight tasks and close the client.

        Cancellation is a process-shutdown action, not evidence about native
        completion. All task exceptions are consumed and the wait is finite.
        """
        self._closing = True
        client, self._client = self._client, None
        tasks = set(self._retained) | set(self._work_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_WAIT_S)
            for task in pending:
                task.cancel()
            for task in tasks:
                _consume_task_exception(task)
        self._retained.clear()
        self._work_tasks.clear()
        if client is not None:
            try:
                await asyncio.wait_for(client.aclose(), SHUTDOWN_WAIT_S)
            except BaseException:  # noqa: BLE001 - shutdown is bounded
                logger.warning("text enhancement client close exceeded its bound")

    # --------------------------------------------------------------- route --

    async def handle(self, request: Request):
        if not self.enabled:
            return _error("enhancement_unavailable")
        if not authorized(request.headers.get("authorization"), self.token):
            return _error("unauthorized")
        if self._completion_unknown:
            # After an unprovable transport loss we cannot rule out a still
            # running native generation. Fail closed until process restart.
            return _error("enhancement_unavailable")
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            return _error("invalid_input")
        if self._slot.locked():
            return _error("enhancement_busy")
        await self._slot.acquire()
        admitted = True
        work = None
        deferred_release = False
        started = time.monotonic()
        deadline = started + self.deadline_s
        try:
            remaining = max(deadline - time.monotonic(), 0.001)
            try:
                body = await asyncio.wait_for(
                    read_bounded_body(request, self.body_limit), remaining)
            except TimeoutError:
                return _error("enhancement_timeout")
            except BodyTooLarge:
                return _error("input_too_large")
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                return _error("invalid_input")
            if not isinstance(payload, dict):
                return _error("invalid_input")
            text = payload.get("text")
            language = payload.get("language", "auto")
            if not isinstance(text, str) or not text.strip():
                return _error("invalid_input")
            if len(text) > self.max_text_chars:
                return _error("input_too_large")
            if language not in LANGUAGES:
                return _error("invalid_input")

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _error("enhancement_timeout")
            await self.start()  # no-op after app startup; safe for bare ASGI use
            # The submitted call gets its OWN finite budget, longer than the
            # client response deadline. A 504 must not shorten the upstream
            # HTTP timeout, otherwise the timeout itself would look like
            # completion while Ollama is still generating.
            work = asyncio.create_task(
                self._ollama_correct(text, self.upstream_timeout_s))
            self._work_tasks.add(work)
            work.add_done_callback(self._work_tasks.discard)
            work.add_done_callback(_consume_task_exception)
            try:
                corrected = await asyncio.wait_for(asyncio.shield(work), remaining)
            except TimeoutError:
                # The native call was submitted and cannot be aborted by the
                # HTTP deadline. Keep the only admission slot until it ends.
                deferred_release = True
                self._retain_release(work)
                return _error("enhancement_timeout")
            except asyncio.CancelledError:
                if not self._closing:
                    deferred_release = True
                    self._retain_release(work)
                raise
            except UpstreamCompletionUnknown:
                self._latch_unknown()
                return _error("enhancement_unavailable")
            except EnhanceError as failure:
                return failure.response()
            except Exception as exc:  # noqa: BLE001 - never leak internals
                logger.warning("text enhancement failed: kind=%s", type(exc).__name__)
                return _error("enhancement_unavailable")

            # `language` is validated for the agreed client contract but is
            # deliberately not injected: the current corrector prompt does the
            # work and must stay behaviour-identical for this migration.
            if corrected == text:
                result = {"status": "unchanged", "text": text,
                          "raw_text": text, "enhanced_text": None}
            else:
                result = {"status": "corrected", "text": corrected,
                          "raw_text": text, "enhanced_text": corrected}
            logger.info("text enhancement ok: status=%s chars_in=%d chars_out=%d",
                        result["status"], len(text), len(corrected))
            return JSONResponse(result)
        finally:
            if admitted and not deferred_release:
                self._slot.release()

    def _retain_release(self, work):
        """Track the one bounded release waiter strongly until it completes."""
        task = asyncio.create_task(self._release_when_done(work))
        self._retained.add(task)
        task.add_done_callback(self._retained.discard)
        task.add_done_callback(_consume_task_exception)

    def _latch_unknown(self):
        """Fail closed after a transport loss with unknown native completion."""
        if self._closing or self._completion_unknown:
            return
        self._completion_unknown = True
        logger.error("text enhancement latched unavailable: upstream completion "
                     "unknown after transport loss; restart the service to recover")

    async def _release_when_done(self, work):
        """Hold admission until submitted work truly finishes (no cancel)."""
        try:
            await work
        except UpstreamCompletionUnknown:
            self._latch_unknown()
        except BaseException:  # noqa: BLE001 - result was already reported/discarded
            pass
        finally:
            self._slot.release()

    # ------------------------------------------------------------ upstream --

    async def _ollama_correct(self, text: str, timeout_s: float) -> str:
        """One Ollama correction round; raises EnhanceError with public code."""
        await self.start()
        options = _enhance_options(len(text))
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": ENHANCER_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            "stream": False,
            "think": False,
            "keep_alive": -1,
            "options": options,
        }
        budget = max(timeout_s, 0.001)
        try:
            # httpx.Timeout is per-operation/inactivity; enforce a real
            # wall-clock cap around the whole submitted request as well.
            status, raw = await asyncio.wait_for(
                self._bounded_post("/api/chat", body, budget), timeout=budget)
        except TimeoutError:
            # The wall-clock budget was exhausted with the request submitted;
            # native completion is not provable from the client cancellation.
            raise UpstreamCompletionUnknown()
        except (httpx.ConnectTimeout, httpx.PoolTimeout):
            # No request reached the model, so no native work is outstanding.
            raise EnhanceError("enhancement_unavailable")
        except (httpx.ReadTimeout, httpx.WriteTimeout):
            # The request may have been submitted and generation may continue.
            raise UpstreamCompletionUnknown()
        except httpx.RequestError:
            # Protocol/read/write loss after submission is not completion.
            raise UpstreamCompletionUnknown()
        except ValueError:
            # Bounded response overflow: the HTTP response was delivered, so
            # generation itself is complete; this is a normal 503 failure.
            raise EnhanceError("enhancement_unavailable")
        if status != 200:
            raise EnhanceError("enhancement_unavailable")
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise EnhanceError("invalid_model_output")
        if not isinstance(payload, dict) or payload.get("done") is not True:
            raise EnhanceError("invalid_model_output")
        count = payload.get("eval_count")
        if payload.get("done_reason") == "length" or (
                isinstance(count, (int, float)) and count >= options["num_predict"]):
            raise EnhanceError("invalid_model_output")
        message = payload.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise EnhanceError("invalid_model_output")
        out = message["content"].strip()
        if not out or len(out) > max(200, len(text) * 3):
            raise EnhanceError("invalid_model_output")
        # A correction may legitimately shrink a little (digits for spelled-out
        # numbers, phonetic spelling) but never half of a sizeable transcript.
        if len(text) >= 300 and len(out) < len(text) * 0.5:
            raise EnhanceError("invalid_model_output")
        return out

    async def _bounded_post(self, path: str, body: dict, timeout_s: float):
        """POST through the app client with a bounded streamed response."""
        chunks = bytearray()
        async with self._client.stream(
                "POST", path, content=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"},
                timeout=httpx.Timeout(timeout_s)) as response:
            status = response.status_code
            async for chunk in response.aiter_bytes():
                if len(chunks) + len(chunk) > self.max_response_bytes:
                    raise ValueError("upstream response too large")
                chunks.extend(chunk)
        return status, bytes(chunks)
