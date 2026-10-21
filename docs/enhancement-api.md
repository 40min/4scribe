# Standalone text enhancement API (`POST /4scribe/v1/text/enhance`)

This endpoint ports the existing parakeet-test text corrector to the standalone
4scribe service. It is a **behaviour-preserving migration**: the current
corrector model, prompt, sampling options and response guards are unchanged.
Prompt/model quality work is deliberately out of scope for this task.

It is a text-only route. It does not touch audio upload, realtime sessions,
the batch endpoint, the glossary, deployment or the legacy service.

## Contract

| Item | Value |
| --- | --- |
| Method / path | `POST /4scribe/v1/text/enhance` |
| Auth | `Authorization: Bearer <FSCRIBE_ENHANCEMENT_TOKEN>` - a **dedicated** key |
| Content type | `application/json` |
| Body | `{"text": "<non-empty string>", "language": "auto" \| "ru" \| "en"}` |
| Text limit | 4 000 characters, rejected (never truncated) |
| Raw body limit | 32 KiB measured on the streamed bytes (Content-Length is advisory) |
| Deadline | 5 s total, including slow upload/body read and the Ollama call |
| Admission | one active enhancement, no queue; a second concurrent request gets `429` |
| Upstream response cap | 512 KiB streamed bytes |

`language` defaults to `auto`. It is validated for the client contract but is
not injected into the current prompt, exactly like the migrated corrector:
supplying `ru`/`en` does not mandate a rewrite or translation.

### Success (`200`)

```json
{
  "status": "corrected",
  "text": "Привет, мир.",
  "raw_text": "привет мир",
  "enhanced_text": "Привет, мир."
}
```

An unchanged result (the guarded model output equals the input) is:

```json
{
  "status": "unchanged",
  "text": "Привет мир",
  "raw_text": "Привет мир",
  "enhanced_text": null
}
```

- `text` is the Loqui-compatible display string: the corrected text when
  `status` is `corrected`, otherwise the original input.
- `raw_text` is always the exact original request string (including original
  whitespace/case).
- `enhanced_text` is the corrected text when `status` is `corrected`, otherwise
  `null`. It is never a fabricated value on an unchanged result.

On any failure the client should keep/use the raw transcript it already has.
The endpoint never returns a partial or truncated correction.

### Errors

```json
{"error": {"code": "enhancement_timeout"}}
```

Codes and statuses are the migrated implementation's codes:

| Status | Code | Meaning |
| --- | --- | --- |
| 401 | `unauthorized` | missing/wrong Bearer (constant-time compare) |
| 413 | `input_too_large` | raw body over 32 KiB or text over 4 000 chars |
| 422 | `invalid_input` | malformed/non-object JSON, non-string or empty/blank `text`, bad `language`, wrong content type |
| 429 | `enhancement_busy` | another enhancement request owns the single slot |
| 502 | `invalid_model_output` | malformed/truncated/empty/oversized or implausibly collapsed model output |
| 503 | `enhancement_unavailable` | endpoint not configured, upstream non-200 or transport failure |
| 504 | `enhancement_timeout` | 5 s total deadline exceeded (body read or model wait) |

## Authentication and configuration

- `FSCRIBE_ENHANCEMENT_TOKEN` is a dedicated Bearer. The route accepts **only**
  this key: the realtime WS token and `FSCRIBE_MOBILE_TOKEN` do not authenticate
  it.
- The endpoint is disabled and fails closed with `503` when the token is unset,
  a known placeholder, shorter than 16 characters, or equal to the WS/mobile
  token. There is no fallback to a test/default/legacy key.
- Auth is checked before any body bytes are consumed.
- Production tokens are provisioned outside this repository; no production
  secret is read, generated or committed here. Local development uses a fake
  value in the git-ignored `.env` only.
- `FSCRIBE_ENHANCER_MODEL` may override the migrated default `qwen3:0.6b`;
  `FSCRIBE_ENHANCEMENT_DEADLINE_S` may override the 5 s deadline for local
  testing. Changing the model is an operational choice, not part of this port.

## Corrector behaviour (migrated unchanged)

- Ollama `POST /api/chat`, `stream=false`, `think=false`, `keep_alive=-1`.
- Options: `temperature=0`, `num_ctx=4096`, and
  `num_predict = 120` for inputs up to 200 chars, otherwise
  `min(1024, len(text) * 4 // 5)`.
- Response checks: HTTP 200; JSON object; `done=true`; not `done_reason=length`;
  `eval_count` below `num_predict`; `message.content` is a string; stripped
  output is non-empty, at most `max(200, len(text)*3)` chars, and for inputs of
  300+ chars not less than half the input length.
- The system prompt is the current parakeet-test prompt verbatim.

Known limitations of the migrated behaviour (documented, not fixed here):

- It is a small `qwen3:0.6b` model; corrections may be imperfect, and the
  "never drop or add words" / fidelity intent is a prompt instruction, not a
  hard guarantee. Punctuation, capitalization, number formatting and phonetic
  brand-term normalization are best-effort.
- The guard only detects empty, oversized, visibly truncated or heavily
  collapsed output; a plausible but wrong rewrite is not detected.
- Cold model loading may exceed the 5 s deadline, producing `504`; clients must
  fall back to raw text.
- `language` does not pin or override model behaviour in this port.

## Lifecycle and resource bounds

- One separate `asyncio.Semaphore(1)` serializes only text enhancement. It is
  **not** the ASR/decode semaphore, so application locks do not serialize
  correction against transcription or batch work. Both still share the same
  physical Ollama/CPU resources, so real CPU contention remains possible; this
  task does not add a scheduler.
- A timeout or HTTP/client cancellation does **not** abort native Ollama
  generation. The single admission slot remains held until the submitted
  coroutine actually completes; meanwhile new requests receive `429`. No retry
  loop or unbounded background task is created.
- The async `httpx.AsyncClient` is created on application startup and closed on
  shutdown by the app lifecycle (never a module-level client).
- A slow authenticated upload is bounded by the same total deadline and, once
  it times out before any upstream submission, releases the slot normally.
- Logs contain status codes and character counts only; no transcript text,
  audio or tokens.

## Out of scope

No batch/realtime inline enhancement, no language/glossary/new-model
experiments, no Loqui migration, no deployment, routing, secrets or legacy
service changes in this stage. A later task may reuse this corrector seam with
raw-text fallback.
