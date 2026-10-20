# Accepted container operations

As of 2026-10-20, `fourscribe-container` is active on host loopback port 8093.
Owner live acceptance was confirmed that day. The accepted immutable image is
`sha256:7708a83bff51186ec273612b63fe6e6575fb46971e49e74a5c226610791e6517`,
with persistent local tag `4scribe:accepted-2026-10-19`.

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

Future fixes, dependency installation and tests use container images. Core mock
checks can run using the README command. Use isolated candidate images for
changed code and owner-accepted deployment; never install a replacement host
virtualenv. Retained public smoke and replay utilities need their Python
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

## Residual scratch cleanup (owner/root, pending)

The reviewed exact-path cleanup for the remaining old evaluation/session scratch
is `deployment/cleanup-residual-scratch.sh`. It first verifies the accepted
healthy container and its sole read-only model-cache mount, rejects symlinks,
mounts and live process references, then removes only the named historical
scratch directories. It does not touch the container, image, model cache,
secrets, repo, Loqui reference, private owner replay copies or the unknown
parakeet test file.

```sh
sudo /opt/40zen/services/4scribe/deployment/cleanup-residual-scratch.sh
```

Private owner replay directories remain pending explicit owner proof because
their originals could not be verified without reading private content.
