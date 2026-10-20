# 4scribe

Local streaming speech-to-text for Loqui. Python/FastAPI serves revisable partial
transcripts, completed segments and word timestamps using Qwen3-ASR through
Ollama and Qwen3-ForcedAligner on CPU. English or Russian is selected per session.

This preserves the current implementation, not a portable production-ready
release. Latency and noise-triggered hallucinations remain under investigation.

## Layout

- `src/fourscribe`: service, bounded queues, endpointing, ASR, alignment and optional sanitized lifecycle tracing.
- `tests`: synthetic/mock protocol and regression tests; literal tokens are dummy credentials.
- `deployment/smoke_wss.py`: authenticated streaming smoke test.
- `deployment/compose.yaml`: accepted active container runtime.
- `deployment/retire-host-runtime.sh`: guarded owner/root removal of the obsolete host runtime.
- `deployment/cleanup-residual-scratch.sh`: exact-path owner/root cleanup of residual historical scratch.
- `docs/container-permanent.md`: current container operations and host retirement status.
- `docs/backend-eval-manifest-2026-10-16.md`: preserved public backend-eval versions/fixture provenance.
- `deployment/diagnostics`: standalone synthetic lifecycle diagnostic harness.
- `deployment/replay/streaming_replay.py`: original-paced saved-audio replay utility.
- `docs/pip-freeze-2026-10-16-completion.txt`: historical environment inventory, not a portable lockfile.

Model weights, virtual environments, recordings, transcripts, operational reports,
credentials and machine-specific service files are intentionally excluded.
Local research artifacts remain preserved outside Git.

## Runtime

The accepted container uses Python 3.14.4, FastAPI, Uvicorn, httpx, websockets,
CPU torch, transformers and qwen-asr. Provision dependencies and models separately;
there is no automatic model download at service startup. CPU torch requires its
appropriate package index. The tested Ollama package is the third-party Q8_0
`frozenlab/qwen3-asr:1.7b`. The aligner is `Qwen/Qwen3-ForcedAligner-0.6B`, loaded
from a pre-provisioned local Hugging Face cache. Model licenses apply separately.

Installation and maintenance use the container image and Compose definition; see
`docs/container-permanent.md`. The host virtualenv and systemd service are retired.

Environment-driven startup rejects the dummy `test-token` default. TLS/ingress
and service management are deployment responsibilities. WebSocket paths:
`/v1/audio/transcriptions/realtime` and
`/4scribe/v1/audio/transcriptions/realtime`.
Authenticate with `Authorization: Bearer <token>`. Send `session.update` with
language `en` or `ru`, followed by binary PCM16 mono 16 kHz audio. A commit flushes
finals before its ACK; cancellation invalidates pending results. Health endpoints:
`/healthz` and `/readyz`.

Configuration lives in `src/fourscribe/config.py`, including
`FSCRIBE_OLLAMA_URL`, `FSCRIBE_MODEL`, `FSCRIBE_INFERENCE_WORKERS`,
`FSCRIBE_ALIGNER_HF_HOME`, `FSCRIBE_ALIGNER_ENABLED` and bounded queue sizes.
Some historical default paths reference the original experimental environment.
A host-specific pipeline diagnostic and its dependent test remain local-only.

## Container deployment (active)

`fourscribe-container` serves `127.0.0.1:8093` using the accepted image
`sha256:7708a83bff51186ec273612b63fe6e6575fb46971e49e74a5c226610791e6517`
(tag `4scribe:accepted-2026-10-19`). Owner live acceptance was confirmed on
2026-10-20. The host unit and obsolete virtualenv were removed; residual
historical scratch cleanup is pending an exact-path owner/root script. Host
rollback is retired; future fixes use containers. The root-owned external env
file, read-only aligner cache, external Ollama and Tailscale route remain in use.
See `docs/container-permanent.md`.

Pre-decode speech verification uses pinned `silero-vad==6.2.3` (MIT, bundled
JIT model). Env-driven startup enables it by default; disable with
`FSCRIBE_SILERO_ENABLED=0`. It runs on the exact segment PCM before ASR for
partials and finals; rejection emits no text and no aligner call, but a commit
still ACKs and the clock continues. If enabled and the verifier cannot load,
startup fails explicitly. See `docs/silero-predecode-trial.md`.

A completed ASR response with empty/whitespace content (or the official
`language None<asr_text>` no-speech marker) is valid no-transcription, not an
error: no final, no aligner call, commit still ACKs. Missing/malformed fields,
`done=false`, and HTTP/transport failures remain errors.

## Tests

Core mock tests need no model server or downloads:

```sh
docker exec fourscribe-container python -m pytest tests -q -p no:cacheprovider \
  --ignore=tests/test_aligner_cache.py \
  --ignore=tests/test_diag_units.py \
  --ignore=tests/test_trace_replay.py
```

The full suite additionally needs external Loqui sidecar source used by replay
and diagnostic harnesses, and the locally provisioned aligner snapshot for cache
resolution tests. Replay supports `FSCRIBE_REPLAY_SIDECAR`; the older diagnostic
harness references its dated local sidecar snapshot. These are portability
limitations, not bundled dependencies. No owner recordings are included.
