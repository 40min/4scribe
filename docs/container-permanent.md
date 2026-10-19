# 4scribe permanent container deployment (prepared — NOT ACTIVE)

Status: **PREPARED / NOT ACTIVE**. The accepted container image exists locally
from the owner trial, but the production listener is still the existing
`fourscribe.service` on `127.0.0.1:8093`. No cutover, restart, service disable,
Tailscale change or secret read was performed while preparing these artifacts.

## Accepted basis

| Item | Value |
| --- | --- |
| Accepted commit | `948073011112e3cd164ca8269388bc709e1cfa69` |
| Accepted image ID | `sha256:7708a83bff51186ec273612b63fe6e6575fb46971e49e74a5c226610791e6517` |
| Image build | Python 3.14.4, CPU torch 2.14.1, Silero 6.2.3; no CUDA/ONNX; aligner weights not baked |
| Trial image tests | 83 in-image tests passed |
| Public two-stream trial | RU: 3 partials / 24-word final; EN: 1 partial / 17-word final; word bounds, monotonicity, final-before-ACK and auth-negative 403 passed |
| Trial memory peak | 842,010,624 bytes cgroup peak, excluding the external Ollama process |
| Trial endpoint | isolated `127.0.0.1:18093`, host network; removed after trial |

The trial evidence and the accepted image ID are the deployment basis. No new
benchmark or repetition is required for this cutover because the image and
source pins are unchanged.

## Runtime shape

`deployment/compose.yaml` defines one service:

- **Image:** persistent local tag `4scribe:accepted-2026-10-19`, created only
  from the accepted immutable ID above. `pull_policy: never`; the image is not
  uploaded to a registry. The cutover script verifies the tag's ID before use
  and verifies the running container's image ID again.
- **Network:** `network_mode: host`; uvicorn binds `127.0.0.1:8093` via explicit
  CLI `--port 8093`. No port mapping and no change to the host Ollama at
  `127.0.0.1:11434`.
- **Identity:** numeric non-root `10001:10001`, supplementary group `1000` for
  read-only model-cache access.
- **Filesystem:** read-only root, writable `tmpfs` only at `/tmp`, aligner cache
  mounted read-only at `/models/hf` from
  `/opt/40zen/services/4scribe/model-cache/hf`.
- **Secret:** root-owned `env_file` `/opt/40zen/secrets/4scribe.env`; `FSCRIBE_TOKEN`
  is never baked, printed, copied or passed on a command line.
- **Restart:** `unless-stopped`.
- **Stop:** `stop_grace_period: 20s` plus uvicorn
  `--timeout-graceful-shutdown 10`; both are bounded best-effort unwinding, not
  a final-ACK guarantee.
- **Security:** `no-new-privileges`.
- **Healthcheck:** `/healthz` only, no auth token and no model load. This is
  liveness/startup evidence, not full readiness.

The existing Tailscale Serve handler and public URL are unchanged. The container
binds the same loopback port the host unit used.

## Owner cutover command

Run as root/owner after review and when no recording is active:

```bash
sudo /opt/40zen/services/4scribe/deployment/cutover-container.sh
```

Prerequisites enforced by the script:

1. root access, Docker CLI + daemon, Docker Compose v2, `ss`, `git`, `curl`;
2. accepted image ID present locally (the script does not pull or rebuild);
3. persistent tag absent or already resolving to that exact ID;
4. runtime source/build inputs identical to the accepted commit and clean;
   repository ownership is handled only by a task-local
   `git -c safe.directory="$ROOT"` wrapper (no global Git config, no wildcard
   trust);
5. `ROOT` resolves to the canonical service path
   `/opt/40zen/services/4scribe`; other paths are rejected;
6. aligner cache group `1000` with group read/execute on the path and group read
   on the probe file;
7. Docker enabled at boot (`docker.service` or `docker.socket`);
8. no established `:8093` connection, and `ss` must be able to decide that;
9. host unit `fourscribe.service` active and enabled as the rollback baseline;
10. any pre-existing `fourscribe-container` carries the 4scribe ownership label
    and accepted image ID; foreign containers are never touched.

## Switch semantics

1. Validate compose config quietly (`config -q`); no interpolated secret output.
2. Re-check idle state, then `systemctl disable --now fourscribe.service` so the
   host unit cannot race the container or start a second listener at boot.
3. Wait (bounded) for `:8093` to be released.
4. Start the container with `docker compose up -d --force-recreate`.
5. Verify ownership label, running image ID, `/healthz` status 200 and the
   expected `aligner=qwen3-forced-aligner-0.6b`,
   `verifier=silero-vad-6.2.3`, `word_timestamps=true`; verify the host unit is
   inactive and disabled.

Success is reported as `cutover_result=active` with
`readiness_scope=startup_health_only`. It does **not** claim `/readyz` full
readiness: `/readyz` only checks Ollama `/api/tags`, and the aligner loads lazily.
The accepted authenticated real-aligner/two-stream smoke remains the decode
evidence; it is not repeated by the cutover script.

The host unit's close path cancels in-flight session state, and container
SIGTERM has the same limitation: neither flushes a pending final/ACK. That is why
the switch is idle-only and why the bounded stop grace is not described as a
drain guarantee.

The script is idempotent: if the owned container is already running with the
accepted image and healthy, it reports `already_active` and only ensures the host
unit is disabled.

## Failure rollback (automatic)

Any failure after the host unit is stopped invokes the script's rollback:

1. stop only containers with `com.40zen.service=4scribe` (never a foreign
   container);
2. re-enable the host unit only if it was previously enabled;
3. start `fourscribe.service`;
4. bounded host `/healthz` check and explicit `rollback_result`.

The container is stopped, not removed, so logs remain available. The image tag,
host venv, model cache and token file are preserved.

After the switch is armed, `SIGINT`/`SIGTERM` route once through the same
rollback; the signal traps are disarmed first, so a second signal cannot
recurse. Before the switch is armed, a signal exits without changing anything.
During rollback itself, signals are disarmed and a further signal may interrupt
it; re-running `cutover-container.sh` (which validates the baseline) or the
manual rollback command is the recovery path. No further shutdown architecture is
introduced.

## Manual rollback

Run as root/owner, idle-only:

```bash
sudo /opt/40zen/services/4scribe/deployment/rollback-container.sh
```

It refuses while any `:8093` session is established, stops only the labeled
owned container (leaving it in place), re-enables and starts the host unit, and
verifies `/healthz`. The Tailscale route is unchanged throughout.

## Boot and unit policy

- While the container is active, `fourscribe.service` is disabled; Docker's
  `unless-stopped` policy is the single boot path. No duplicate systemd unit is
  created for the container.
- `systemctl status fourscribe.service` should show inactive/disabled after
  cutover; `docker inspect` should show the container healthy.
- If Docker is not enabled at boot, cutover fails rather than leaving a
  non-restarting service.

## Explicit non-goals

- No Tailscale Serve, token, Loqui, Ollama or other STT service changes.
- No image rebuild, upgrade, registry upload, or model download/bake.
- No deletion of the host venv, model cache, token, trial evidence, container
  logs or historical trace files.
- No SIGTERM/final-ACK guarantee; planned switches are idle-only.

## Preparation verification (agent-side, without Docker)

- `bash -n` passes for both scripts.
- Compose YAML parses and shows the expected image, host network, user, mounts,
  read-only/tmpfs, restart policy, stop grace, labels and healthcheck.
- Both scripts fail closed with the expected message when run without root.
- All repository inspections in the cutover script use the task-local
  `repo_git()` wrapper (`git -c safe.directory="$ROOT" -C "$ROOT"`); there are no
  direct `git -C` calls and no global Git ownership configuration. The service
  root must canonicalize to `/opt/40zen/services/4scribe`; a `/tmp` path is
  rejected by the static check.
- A tiny trap-pattern test confirmed one rollback call on `TERM` after arming
  (no recursion) and exit 130 without rollback before arming.
- The healthcheck Python expression is syntactically valid.
- Docker daemon checks, image-ID verification, model permission probe and live
  cutover/rollback remain **unrun** because `opencode` has no Docker/sudo access.
