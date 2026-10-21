# Accepted container operations

As of 2026-10-21, `fourscribe-container` is active on host loopback port 8093.
The accepted immutable image is
`sha256:f313ab3a8c98696c611fc8ee88e0a9f8e858aad3f1d2442363fd27fb50daa474`,
with persistent local tag `4scribe:accepted-enhancement-routefix-2026-10-21` (source `e9ca825`).
Promotion passed batch auth isolation and the Loqui WebSocket handshake. The
owner confirmed HardAfanasii's optional 4scribe provider implemented, tested and
deployed. Earlier Loqui live acceptance was confirmed on 2026-10-20.

Development/source checkout: `/opt/40zen/repos/4scribe`. Active runtime Compose
and cache remain under `/opt/40zen/services/4scribe`. Batch uses
`/4scribe/v1/audio/transcriptions` and separate `FSCRIBE_MOBILE_TOKEN`; streaming
keeps `FSCRIBE_TOKEN`. Both are external root-only env entries. Batch has no
text enhancer. Loqui streaming, optional HardAfanasii batch and standalone
enhancement all use this one 4scribe server. The legacy Parakeet/Nemotron stack
was retired permanently on 2026-10-21, including its containers, models, images,
routes and dedicated credentials; no backup or rollback copy was retained.
Inline batch/WS enhancement flags remain **To-do** and are not enabled.

The container includes Python, the app, Silero VAD and forced aligner runtime.
Its sole bind mount is `/opt/40zen/services/4scribe/model-cache/hf` → `/models/hf`
(read-only). Preserve this cache, the accepted image, Compose definition and
`/opt/40zen/secrets/4scribe.env` (root-owned 0600; never print/copy it).
Ollama remains external at loopback 11434 using `frozenlab/qwen3-asr:1.7b`.
UID/GID are 10001:10001 with supplementary group 1000; networking is host mode.
Docker restart policy is `unless-stopped`. No production STT/container change
was made during host retirement or residual-scratch preparation.

## Checks and future maintenance

```sh
docker inspect --format '{{.Image}} {{.State.Status}} {{.State.Health.Status}} {{.RestartCount}}' fourscribe-container
curl -fsS http://127.0.0.1:8093/healthz
```

Expected health: `ok=true`, aligner `qwen3-forced-aligner-0.6b`, verifier
`silero-vad-6.2.3`, word timestamps true. Health proves liveness, not an
authenticated audio/decode session; owner live acceptance is separate evidence.

Production fixes use candidate container images and owner-accepted deployment.
Development can use the authorized ignored repo `.venv` (~1.7 GB) and
`.dev-tools` (~515 MB), including ffmpeg. These are development-only, not a
replacement host service. Core image checks use the README command. Retained public smoke and replay utilities need their Python
dependencies in a container and explicit read-only fixture mounts; owner
recordings are not bundled. `container-trial.sh` retains its isolated public
trial workflow using host Python stdlib helpers; fixture fetch and smoke run in
the accepted image, not a host virtualenv. It is not part of normal production
startup.

Compose operations use `deployment/compose.yaml` and require access to the
root-owned env file. Service recreation/restart is a separate deployment action;
none is needed for this cleanup. Do not dump Docker environment/secret values.

## Host retirement: complete

The obsolete `fourscribe.service` and its empty drop-in directory have been
removed, and the legacy `.venv` (pre-delete inventory 1,699,748,165 apparent
bytes) is absent. Host rollback is permanently retired; the migration
cutover/rollback tools and the obsolete local unit template were removed.
Historical trial/plan records remain. The accepted container remains the only
runtime; no model, image, secret or repo content was removed by host retirement.

## Residual scratch cleanup: verified, final leftovers pending

The owner executed `deployment/cleanup-residual-scratch.sh` on 2026-10-20. Its
exact targets were re-checked on 2026-10-20 and are absent: `/tmp/4scribe-backend-eval`,
the four `/tmp/4scribe-container-trial.*` directories, the two completion/
handoff directories, `.codex-worker` and the empty `/var/lib/fourscribe-trace`.
The historical `cleanup-residual-scratch.sh` and `retire-host-runtime.sh` are
one-shot tools whose runs are complete; the final reviewed script removes them
with the remaining named leftovers.

`deployment/cleanup-final-leftovers.sh` is a stale historical draft: its image
check predates the mobile promotion, and source/runtime paths have since split.
Do not run it unchanged. Re-evaluate exact targets against the current image,
mounts and active processes before deleting any remaining replay copies,
experiments, backups or caches. The local-only resource registry records these
pending candidates; it is not public documentation or an automatic deletion list.

Justified final holds: `deployment/PLAN.md`, `deployment/RESULT.json` and
`deployment/real_aligner_check.py` are retained historical acceptance/
diagnostic evidence. The former `/tmp/opencode/parakeet-test` fixture and the
legacy `services/parakeet`/`parakeet-test` paths were removed with the completed
2026-10-21 STT retirement. Superseded trial/acceptance tags may be removed only
after confirming no container references them; retain the current accepted image
and tag.

## Mobile acceptance evidence — 2026-10-21

Independent image acceptance on source `a34f8ef`: **137 tests passed**, including
real ffmpeg WAV/M4A handling, plus isolated health/auth smoke. The disposable
container was removed. Independent batch scope: **54 passed**. The full host/dev
suite reported **167 passed**. Real Qwen RU/EN/auto and silence integration ran
in the isolated development environment, not inside the acceptance container;
these are distinct evidence scopes. No containerized real-Qwen campaign is
claimed by the image/auth check.

## Standalone enhancement accepted — 2026-10-21

Current source `e9ca825` passed **197 image tests** and **114 focused tests**;
no new full host-suite result is claimed. Public endpoint:
`https://40zen.tail055a00.ts.net/4scribe/v1/text/enhance`, using dedicated
`FSCRIBE_ENHANCEMENT_TOKEN`. Both `/4scribe/v1/text/enhance` and
`/v1/text/enhance` share one handler because ingress strips the prefix.
The original `qwen3:0.6b` corrector is unchanged; raw text stays canonical.
Client deadline is 5 seconds, upstream wall-clock budget 60 seconds, with
fail-closed latch and explicit safe restart recovery. ASR model keep-alive is
`-1` in canonical/runtime Compose. Inline batch/WS enhancement is not enabled.

Owner-accepted Loqui meeting `4dd5a489` (15:46:31–15:47:21 UTC) made two
external enhancement calls: HTTP 200, unchanged outputs of 110/105 characters.
No errors, busy responses, timeouts, latch or container restarts were observed.
This proves integration, not correction-quality improvement. Completed one-shot
promotion scripts were removed; reusable candidate smoke/acceptance remain.
