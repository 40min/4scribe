# Legacy STT retirement — completed

Status: **COMPLETE — owner-executed 2026-10-21**.
Task: `NQf35XIvbfOf` (4scribe · retire old STT stack).
Result: 4scribe is the only local STT/transcription server; legacy
Parakeet/Nemotron runtime, ingress, models, images and credentials are absent.
No rollback copy or legacy backup was retained.

## Outcome evidence

Owner root execution reported `retirement_complete`, legacy STT absent,
`fourscribe-container` healthy, and no rollback copy. Primary independently
verified:

- Current image
  `sha256:f313ab3a8c98696c611fc8ee88e0a9f8e858aad3f1d2442363fd27fb50daa474`
  (`4scribe:accepted-enhancement-routefix-2026-10-21`) running/healthy with
  `RestartCount=0`.
- Health: aligner, Silero, word timestamps, mobile and enhancement all true;
  `completion_unknown=false`.
- Tailscale Serve: only `/` → `127.0.0.1:18789` and `/4scribe` →
  `127.0.0.1:8093`; no global reset.
- No legacy `parakeet-stt`/`parakeet-test` containers, none of the nine legacy
  image tags, legacy directories/fixture absent, and no listeners on 8090/8091.
- Both updated clients owner-accepted: HardAfanasii and Loqui.

## Removed

| Resource | Evidence |
| --- | --- |
| `/opt/40zen/services/parakeet` | Removed; 1,455,587,328 apparent bytes |
| `/opt/40zen/services/parakeet-test` | Removed; 716,800 apparent bytes; local Git history only |
| `/tmp/opencode/parakeet-test/ru_test.wav` | Removed with known legacy fixture directory |
| Containers `parakeet-stt`, `parakeet-test` | Absent |
| Network `parakeet_default` | Absent |
| Legacy images | Nine exact tags / seven image IDs removed, no force/wildcard |
| Serve `/parakeet-test` → 8091 | Removed |
| Serve `/v1/audio/transcriptions/realtime` → 8090 | Removed |
| `/opt/40zen/secrets/stt.env` | Deleted unopened by owner/root |
| `/opt/40zen/secrets/parakeet-mobile.env` | Deleted unopened by owner/root |

Removed image tags: `parakeet-stt:0.2.0-a12b237`, `parakeet-stt:latest`,
`parakeet-stt:0.2.0-b809bbb`, `nemo-speech-runtime-cpu:0.2.0-a12b237`,
`nemo-speech-runtime-cpu:latest`, `nemo-speech-runtime-cpu:0.2.0-b809bbb`,
`parakeet-test:latest`, `parakeet-test:rollback-2026-10-14`,
`parakeet-test:pre-mobile-20261007`.

## Preserved

- `fourscribe-container`, accepted image, Compose definition, read-only model
  cache `/opt/40zen/services/4scribe/model-cache/hf`, and root-only
  `/opt/40zen/secrets/4scribe.env`.
- Shared Ollama and models: `qwen3:0.6b` enhancement, `frozenlab/qwen3-asr:1.7b`,
  `qwen3-embedding:0.6b`, and all unrelated models.
- Development environment `.venv`/`.dev-tools`, runtime paths, public runbook
  receipts, owner recordings, private replay copies and historical docs.
- `/opt/40zen/repos/NeMo-Speech.cpp`: retained as a distinct upstream source
  checkout pending owner decision. It is not a deployed legacy service.

## Consequences and follow-up

- Legacy `/parakeet-test`, raw `/v1/audio/transcriptions/realtime`, ports
  8090/8091, models and dedicated credentials have no recovery path and must not
  be recreated. Historical details remain in Git and dated documents.
- 4scribe streaming, mobile batch and standalone enhancement are unchanged.
  This retirement does not claim model-quality improvement.
- Inline batch/WS enhancement flags remain **To-do** and are not enabled.
- The completed one-shot retirement script, Serve guard and synthetic test were
  removed after execution so no rerunnable cleanup remains.
