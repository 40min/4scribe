# Mobile batch API (HardAfanasii) - `/4scribe/v1/audio/transcriptions`

Ordinary HTTP multipart batch transcription for the HardAfanasii Android client.
It is served by the existing 4scribe app next to the realtime WSS API, on the same
container and port (`127.0.0.1:8093`). It does **not** replace, alias or preserve
the legacy `/parakeet-test/...` route: that service and the current APK keep
running in parallel until a client migration is accepted separately.

No Android code, route, ingress or deployment change is part of this stage.

## Contract

| Item | Value |
| --- | --- |
| Method / path | `POST /4scribe/v1/audio/transcriptions` (loopback alias `POST /v1/audio/transcriptions`) |
| Auth | `Authorization: Bearer <FSCRIBE_MOBILE_TOKEN>` - a **separate** key, never `FSCRIBE_TOKEN` |
| Body | `multipart/form-data`, exactly one part named `file` |
| Format | M4A/AAC (`audio/mp4`, `audio/m4a`, `audio/x-m4a`, `audio/aac`) or WAV (`audio/wav`, `audio/wave`, `audio/x-wav`) |
| Size | file <= 2 MiB, total multipart body <= 2 MiB + 64 KiB envelope |
| Duration | decoded PCM <= 61 s (+0.1 s encoder tolerance), measured on the decoded samples |
| Language | omitted or `auto` = automatic (no assistant prefill); explicit `ru` / `en` validated |

Unknown fields (including the old `enhance` flag and `?enhance=true`) are
accepted and ignored. There is no enhancer, glossary or post-correction call.

### Success (`200`)

```json
{
  "text": "…",
  "enhanced_text": null,
  "enhancer_error": false,
  "language": "auto",
  "audio_duration_ms": 6120,
  "conversion_ms": 44,
  "stt_ms": 980,
  "enhancer_ms": 0,
  "server_total_ms": 1088,
  "rtf": 0.16
}
```

`conversion_ms`, `stt_ms`, `enhancer_ms` and `server_total_ms` are always
finite, non-negative numbers (never `null`); `enhancer_ms` is numeric `0`.
`enhanced_text` is always `null`, so the client's "use enhanced only when
filled and error-free" rule falls back to `text`. Empty/no-speech audio is a
successful `200` with `"text": ""` - not an error.

`language` reports the **requested mode**, not a detected language: `auto` when
the request omitted the field (the ASR client exposes no detected-language
value), otherwise `ru`/`en`. Nothing is translated or forced when omitted.

### Errors (generic bodies, never audio/transcript text)

```json
{"error": "unauthorized", "message": "missing or invalid bearer token"}
```

| Status | Code | Meaning |
| --- | --- | --- |
| 401 | `unauthorized` | missing/wrong Bearer (constant-time compare) |
| 400 | `invalid_request`, `unsupported_audio_format`, `invalid_audio`, `invalid_language` | malformed multipart, unknown container, undecodable audio, unsupported language |
| 413 | `payload_too_large`, `audio_too_long` | upload/body over 2 MiB (+overhead), decoded audio over 61 s |
| 502 | `asr_unavailable` | ASR backend failure |
| 503 | `batch_unconfigured`, `busy` | endpoint not configured (fail closed) or one request already active |
| 504 | `deadline_exceeded` | upload/parse, 15 s conversion or 90 s total deadline exceeded |

## Behaviour and guards

- **Separate key, fail closed.** Batch is enabled only when
  `FSCRIBE_MOBILE_TOKEN` is set, at least 16 characters, not a placeholder and
  **different** from `FSCRIBE_TOKEN`. Otherwise the route answers `503`
  (`batch_unconfigured`); it never falls back to the realtime token or a test
  default. A missing mobile key does not affect WSS startup or auth.
- **Bounded upload.** Auth is checked before any body parsing. `Content-Length`
  is only an early hint: the body is consumed through a running byte cap, so a
  chunked upload cannot bypass the limit; the file part is capped again after
  parsing. The body read **and** multipart parsing are bounded by the remaining
  total deadline, so a slow authenticated upload cannot hold the only batch
  slot: it ends in `504` and the slot (plus any parsed part spools) is released.
  Oversize uploads are rejected with `413` before any decode/ASR work.
- **One active request.** A second concurrent upload gets `503 busy`; nothing is
  queued. ffmpeg decode and ASR reuse the app's shared executor, decode
  semaphore and single-worker Silero verifier, so batch work never occupies
  more than one inference slot next to live WSS traffic.
- **Bounded conversion.** M4A/WAV are decoded by a CPU `ffmpeg` subprocess with
  an explicit demuxer, mono 16 kHz PCM16 output, a 15 s wall clock, a bounded
  stdout (duration-bomb safe) and stderr discarded. The child is killed and
  reaped on timeout, cancellation or output overflow; private temp files
  (`0700` dir, `0600` file) are deleted on every path. Duration is verified on
  the decoded PCM, never on container metadata.
- **61 s gate, no truncation.** Up to 61 s of client audio is accepted. Above a
  40 s ASR window the PCM is split at the lowest-energy point (nearest pause)
  into contiguous windows and the transcripts are stitched verbatim - no
  dropped tail, no LLM rewrite, no invented word de-duplication.
- **Deadline honesty.** A timed-out/cancelled request never frees the inference
  slot while native work (ASR or the Silero verifier) is still running; the slot
  is released only after that worker returns. New uploads are refused (`503`)
  meanwhile, so work cannot stack on top of in-flight inference. The native
  Ollama call itself is not cancellable (existing 600 s HTTP timeout), so in
  that pathological case the `504` is returned after the decode finishes
  instead of pretending otherwise.
- **Spool cleanup.** Every parsed multipart part's spooled file is closed on
  every path - success, size/format/language rejection, wrong number of `file`
  parts, malformed form, deadline and cancellation - not only the selected
  upload.
- **Logging.** Status, sizes and timings only - no audio, transcript or token
  content.

## Remaining deployment work (owner)

1. **Image ffmpeg layer (owner-applied).** `Dockerfile` is owned by `i40min`;
   this agent cannot write it and does not change ownership. The primary
   applied the minimal layer below in the working tree (that Dockerfile change
   is not part of the batch commit). Rebuild and confirm `ffmpeg -version`
   inside the image; image acceptance and deployment remain with the primary.

   ```dockerfile
   WORKDIR /app

   # Mobile batch API: M4A/AAC/WAV uploads are decoded to PCM by a CPU ffmpeg
   # subprocess (bounded, killed/reaped on timeout). ffmpeg is the only new
   # system dependency; nothing is installed on the host.
   RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
   ```

   No `deployment/compose.yaml` change is needed: the image carries ffmpeg, the
   existing `/tmp` tmpfs stays writable (and `noexec`, which is fine - ffmpeg
   lives in `/usr/bin`), and no extra volume is required.
2. **Separate mobile key.** Add `FSCRIBE_MOBILE_TOKEN=<owner-generated, >=16
   chars, different from FSCRIBE_TOKEN>` to the existing root-owned external
   env file (`/opt/40zen/secrets/4scribe.env`). Do not reuse the realtime key;
   the service refuses a shared or placeholder value. No key is created or read
   by this change; `/healthz` reports only `mobile_batch.enabled` / `reason`.
3. **Rebuild and test in Docker** (no host venv, no host installs):

   ```sh
   docker exec fourscribe-container python -m pytest tests/test_mobile_batch.py -q -p no:cacheprovider
   docker exec fourscribe-container python -m pytest tests -q -p no:cacheprovider \
     --ignore=tests/test_aligner_cache.py --ignore=tests/test_diag_units.py \
     --ignore=tests/test_trace_replay.py
   ```
4. **Smoke** (after the owner restarts the container with the new image and
   key; do not do this from this stage):

   ```sh
   curl -sS -X POST http://127.0.0.1:8093/4scribe/v1/audio/transcriptions \
     -H "Authorization: Bearer $FSCRIBE_MOBILE_TOKEN" \
     -F "file=@recording.m4a;type=audio/mp4"
   ```

5. **Android migration** (separate, later stage): point `ZenSttProtocol.ENDPOINT`
   at the new path and configure the mobile key in the app. Until that build is
   accepted, the old service and old APK stay untouched.

## Testing notes

`tests/test_mobile_batch.py` is hermetic (mock Ollama, fake aligner,
deterministic converter stubs, real `python` subprocesses for the bounded-exec
helper) and asserts the Kotlin parser's required field types literally,
including numeric `enhancer_ms = 0`. Two tests use the real `ffmpeg` binary on
locally generated fixtures (`test_real_ffmpeg_decodes_generated_wav`,
`test_real_ffmpeg_rejects_malformed_container`) and are skipped when ffmpeg is
absent; they are pending in the rebuilt image. No model downloads and no
private audio are involved.

## Known limitations

- `language: "auto"` is the requested mode; detected language is not exposed.
- 41-61 s audio is transcribed in up to two windows split at a pause; a
  word straddling a pause-free boundary is the only content risk (no tail loss).
- ASR itself cannot be interrupted mid-call; the slot is retained until the
  worker returns (documented above).
- No word timestamps (aligner is not called for batch), no enhancer, no cloud
  fallback.
