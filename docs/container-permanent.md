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
Docker restart policy is `unless-stopped`. No production change was made during
host retirement preparation.

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

## Permanent host retirement: root action pending

The obsolete `fourscribe.service` is inactive and disabled, with no effective
drop-ins. Its `.venv` is not a container mount. The virtualenv (1,699,748,165
apparent bytes, about 1.7 GiB disk usage) and root-owned unit/empty drop-in directory
remain on disk pending owner credentials. No disk space has yet been reclaimed.
Host rollback is permanently retired; the migration cutover/rollback tools and
the obsolete local unit template were removed. Historical trial/plan records
remain.

After reviewing the script, the owner can run:

```sh
sudo /opt/40zen/services/4scribe/deployment/retire-host-runtime.sh
```

The script checks the accepted healthy container and verifier, inactive/disabled
legacy unit, exact paths, no symlink traversal, no unexpected drop-ins/mounts and
no live process mapping the virtualenv before deleting only the obsolete unit,
empty drop-in directory and `.venv`. It reloads systemd and rechecks the unchanged
container. No host rollback, archives, model deletion, container restart or prune.
After success record the actual reclaimed allocation and removal in the local
resource registry. Preparation could not perform root cleanup because
non-interactive sudo credentials are unavailable.
