# 4scribe isolated container trial (owner-runnable)

Status: artifacts committed; **NOT BUILT / NOT TESTED by opencode** because the
Docker socket is not accessible to the `opencode` user. No production cutover;
the host unit on `127.0.0.1:8093` is unchanged. Review revision 2 adds the
required safety, measurement and reproducibility corrections.

## Artifacts

| Path | Purpose |
| --- | --- |
| `Dockerfile` | one app+Silero+aligner image; external Ollama; pinned `python:3.14.4-slim` digest and `torch==2.14.1+cpu`; `qwen-asr` installed `--no-deps` like the host venv |
| `.dockerignore` | deny-all allowlist: `requirements-container.txt`, `src/`, `tests/`; `**/__pycache__/` and `**/*.pyc` excluded even under src/tests |
| `requirements-container.txt` | direct + critical transitive pins from the accepted host inventory |
| `deployment/fetch_trial_fixtures.py` | checked-in fetcher for three pinned PUBLIC CC BY 4.0 clips (~2 MB: short RU, long 11.35 s RU, EN), hash-verified, PCM16 16 kHz |
| `deployment/container-trial.sh` | fail-closed preflight, build, in-image hermetic tests, isolated 18093 host-network trial, RU+EN smokes, selected measurements, owned-only cleanup |

The image bakes no aligner weights, no token and no host/tailnet identifier.
The 1.8 GB aligner cache is a read-only bind mount; the Silero JIT is part of
its pinned wheel.

## Safety preflight (fail closed)

Before any download/build the script:
1. verifies `docker` CLI + daemon access (otherwise exits; no bypass);
2. verifies a 6 GiB free-disk budget (`df -Pk`), retaining that bound;
3. fails if `ss -H -tn state established 'sport = :8093'` reports any active
   production connection, or if `ss` is unavailable;
4. fails if port 18093 already listens;
5. fails if the generated task container name already exists.

Trial scratch is a fresh `mktemp -d` owned by the invoking user; the token is
generated 0600 there and never printed or passed on argv. The cleanup trap
stops/removes only the container ID returned by this invocation's
`docker run -d`; it never touches a preexisting container.

## Ollama handling

The script records `/api/ps` metadata for the specific ASR model only (no full
list dump) before the trial, runs the container with a finite
`FSCRIBE_KEEP_ALIVE_S=60`, and after stopping the owned container unloads that
model only when it was **not** resident before the trial, `/api/ps` was
readable, and no production 8093 connection is active. Other models are never
touched.

## Measurements

- `cgroup_memory_peak_bytes` is read from `/sys/fs/cgroup/memory.peak` inside
  the trial when readable; otherwise the script prints
  `memory_peak=unmeasured` and a clearly labelled `memory_posthoc_snapshot`.
  A `docker stats --no-stream` value is a snapshot, not peak.
- Torch threading is reported as `torch_threads_default_probe` because
  `docker exec python` starts a NEW process; it is not the running service's
  effective thread count. No runtime code change was made.

## Partial expectations

A ~2.10 s phrase can legitimately close before the first partial cadence (the
partial interval is not a mandatory first-delta delay; the long 11.35 s RU
fixture produced its first delta at ~1.1 s). `deployment/smoke_wss.py` therefore
has an explicit `--allow-no-deltas` mode for short final-only fixtures, while
final-nonempty, word-match, finals-before-ACK and authentication-negative
checks stay mandatory. The container trial requires partials on the pinned
11.35 s RU fixture and allows no deltas for the shorter EN run.

## Exact owner command

Run as `i40min` (the docker-group member); no `sudo` needed if already that
user:

```bash
/opt/40zen/services/4scribe/deployment/container-trial.sh
```

From another account, the owner/primary may use
`sudo -u i40min /opt/40zen/services/4scribe/deployment/container-trial.sh`.
The trial binds only `127.0.0.1:18093` via `--network=host`; host Ollama at
`127.0.0.1:11434` is used unchanged. Receipts (token file, health JSON, bounded
smoke logs, ollama ps receipt) are printed as `trial_receipts=<mktemp dir>` and
removed with the container on exit.

## Known blocker

`opencode` is not in the host `docker` group (`docker:x:983:i40min`) and cannot
read `/var/run/docker.sock`; `docker` commands fail with "permission denied".
No credential or permission bypass was attempted.

## Rollback/cleanup

The script self-cleans its own container. If it is ever interrupted before the
trap, the owner can remove the exact named container/id and then
`docker rmi 4scribe-trial:local`. Cutover to 8093 is a separate owner gate and
is not authorized here.
