# Legacy STT retirement plan

Status: **CLIENTS ACCEPTED — READY FOR OWNER ROOT EXECUTION — NOT EXECUTED**
Prepared: 2026-10-21
Client acceptance: 2026-10-21 — owner explicitly confirmed the updated Loqui and
HardAfanasii builds are installed and work as expected; Dart tasks
`QUfX5DkfQlOp` and `rJXb45dp2zWg` are Done. Local agents are finalizing client
commits; server deletion is still pending.
Correction review: 2026-10-21 — Serve handler objects and exact nine image tags
verified by primary; primary independently re-ran the 8 unit tests and
`--check` on `9eb8ec3`.
Task: `NQf35XIvbfOf` (4scribe · retire old STT stack)  
Repo: `/opt/40zen/repos/4scribe`, `main` = `9eb8ec3` at this update
Scope owner: this document, `deployment/retire-legacy-stt.sh`, the repo
allowlist, and a local-only append to
`/opt/40zen/services/4scribe/RESOURCE-REGISTRY.md`.

This plan retires the legacy Parakeet/Nemotron STT runtime. The client gate is
now satisfied; no server deletion has been performed. Final execution remains
an explicit owner root command using the existing reviewed script.

## Decision and gate

- Owner decision: retire the full legacy stack before optional enhancement flags.
- Standalone 4scribe text API and the Loqui migration are **DONE and
  owner-accepted**. Current live image:
  `sha256:f313ab3a8c98696c611fc8ee88e0a9f8e858aad3f1d2442363fd27fb50daa474`
  (`4scribe:accepted-enhancement-routefix-2026-10-21`), container
  `fourscribe-container`, host loopback `127.0.0.1:8093`, healthy WS/mobile/text,
  `FSCRIBE_KEEP_ALIVE_S=-1`.
- **Client gate:** satisfied 2026-10-21. Owner confirmed both updated clients
  are installed and work as expected; Dart tasks `QUfX5DkfQlOp` and
  `rJXb45dp2zWg` are Done. Local agents are finalizing client commits; no server
  deletion has occurred.
- **Independent review:** primary reviewed `9eb8ec3`, re-ran the 8 stdlib unit
  tests and `--check`, and confirmed the selected live Docker inventory
  (containers, mounts, network) and exact nine image tags. Readable live Compose
  references show `stt.env` only for the Parakeet engine + wrapper and
  `parakeet-mobile.env` only for the wrapper. Unrelated Coolify artifact/root
  paths were inaccessible, so this is not a universal negative; no key values
  were read.
- **OpenClaw:** owner confirmed it is not a consumer.
- **Execution gate:** owner runs the existing reviewed command:
  `sudo env RETIRE_LEGACY_STT=YES
  /opt/40zen/repos/4scribe/deployment/retire-legacy-stt.sh --execute
  --clients-accepted`. The script also requires `--clients-accepted` and
  `RETIRE_LEGACY_STT=YES`; there is no automatic or scheduled execution.

## Exact legacy inventory

### Service directories (delete)

| Path | Size / exact contents | Notes |
| --- | --- | --- |
| `/opt/40zen/services/parakeet/` | `du` 1.4 GB | Production Nemotron engine Compose stack |
| `Dockerfile` | 1,825 B | Thin NeMo-Speech healthcheck wrapper |
| `docker-compose.yml` | 1,831 B | `parakeet-stt`, `100.72.25.9:8090:8080`, `stt.env`, Nemotron bind |
| `docker-compose.yml.bak-20261011` | 1,320 B | Old TDT rollback Compose; legacy backup, not retained |
| `README.md` | 10,103 B | Legacy runbook; historical content remains in docs/history |
| `models/nemotron-3.5-asr-streaming-0.6b.q8_0.gguf` | 741,548,352 B | Legacy engine model; metadata only, weights not read |
| `models/parakeet-tdt-0.6b-v3.q8_0.gguf` | 713,975,456 B | Retained legacy rollback artifact; metadata only |
| `scripts/build.sh` | 1,864 B | Builds legacy runtime/wrapper |
| `scripts/smoke-test.sh` | 1,766 B | Reads `stt.env`; must not be run as retirement tooling |
| `scripts/verify-model.sh` | 2,021 B | Verifies legacy GGUFs |
| `/opt/40zen/services/parakeet-test/` | `du` 700 KB | Wrapper/playground/Android adapter; local Git repo |
| `Dockerfile` | 328 B | Python 3.12 wrapper |
| `Makefile` | 2,729 B | **Do not run `make clean`**: uses global Serve off/reset patterns |
| `docker-compose.yml` | 1,090 B | `parakeet-test`, host network, loopback 8091, two env files |
| `app/main.py` | 19,689 B | Browser/mobile routes, ffmpeg conversion, Ollama enhancer |
| `app/static/index.html` | 6,986 B | Browser playground; owner authorized deletion |
| `backups/2026-10-07-before-mobile-stt/app/main.py` | 7,123 B | Legacy code backup |
| `backups/2026-10-07-before-mobile-stt/docker-compose.yml` | 882 B | Legacy code backup |
| `backups/2026-10-11-before-streaming-swap/main.py` | 11,564 B | Legacy code backup |
| `tests/enhancer_check.py` | 2,758 B | Legacy enhancer test |
| `tests/test_enhance.py` | 14,245 B | Legacy enhancer tests |
| `tests/test_mobile.py` | 5,327 B | Legacy mobile tests |
| `.git/` | local only, no remote configured | Local wrapper history; deleted with the directory |
| `__pycache__/`, `tests/.pytest_cache/` | generated | Deleted with the directory |

No owner recordings are in these paths. The `backups/` entries are code/Compose
snapshots, not audio. The historical docs already preserve the wrapper contract
and enhancer prompt; no new backup copy is made.

### Known legacy temporary fixture (delete)

| Path | Size | Why |
| --- | --- | --- |
| `/tmp/opencode/parakeet-test/ru_test.wav` | 431,404 B | Default RU fixture of the legacy smoke test; not an owner recording |
| `/tmp/opencode/parakeet-test/` | directory | Removed only if empty after the fixture is removed |

No broad `/tmp` scan is part of this plan.

### Containers, network, images (delete)

| Resource | Ownership / exact facts | Verification |
| --- | --- | --- |
| container `parakeet-stt` | Compose project `parakeet`, image tag `parakeet-stt:latest`, port `100.72.25.9:8090 -> 8080`, one read-only Nemotron bind, `stt.env`, `restart: unless-stopped` | Primary verified labels, mount and `parakeet_default` membership; exact image ID resolved by script preflight |
| container `parakeet-test` | Compose project `parakeet-test`, image tag `parakeet-test:latest`, host network, loopback 8091, no mounts, `stt.env` + `parakeet-mobile.env` | Primary verified labels, host network and no mounts; exact image ID resolved by script preflight |
| network `parakeet_default` | Default Compose bridge for `parakeet`; `parakeet-test` uses host network, so it has no project network | Primary verified single legacy endpoint `parakeet-stt` |
| image `parakeet-stt:0.2.0-a12b237` | Legacy engine/wrapper tag | Verified exact tag |
| image `parakeet-stt:latest` | Legacy engine/wrapper tag | Verified exact tag |
| image `parakeet-stt:0.2.0-b809bbb` | Legacy engine/wrapper rollback tag | Verified exact tag |
| image `nemo-speech-runtime-cpu:0.2.0-a12b237` | Legacy base runtime tag | Verified exact tag |
| image `nemo-speech-runtime-cpu:latest` | Legacy base runtime tag | Verified exact tag |
| image `nemo-speech-runtime-cpu:0.2.0-b809bbb` | Documented legacy runtime rollback tag | Verified exact tag |
| image `parakeet-test:latest` | Legacy wrapper tag | Verified exact tag |
| image `parakeet-test:rollback-2026-10-14` | Legacy wrapper rollback tag | Verified exact tag |
| image `parakeet-test:pre-mobile-20261007` | Legacy wrapper pre-mobile tag | Verified exact tag |

No named Docker volumes are declared. The only engine bind is the Nemotron GGUF
listed above; the TDT GGUF is on disk but not mounted by the current Compose.
The script deletes only these **nine exact tags** in `LEGACY_IMAGE_TAGS`; no
repository wildcard, `--force`, prune, or broad image cleanup.

### Dedicated credential paths (delete paths only; never read values)

| Path | Role | Usage check |
| --- | --- | --- |
| `/opt/40zen/secrets/stt.env` | `NEMO_SPEECH_HTTP_API_KEY`; engine + wrapper, and legacy smoke test | Bounded search found only legacy compose/README/smoke, historical docs/registry, and the historical `RESOURCE-SCAN`. No systemd or 4scribe live reference. |
| `/opt/40zen/secrets/parakeet-mobile.env` | `MOBILE_STT_API_KEY`; legacy wrapper | Bounded search found only legacy wrapper compose and historical docs. |

`opencode` cannot stat the secret directory. Primary's readable-config review
found no non-legacy live Compose reference; unrelated Coolify artifact/root paths
were inaccessible, so no universal negative is claimed and no key values were
read. The reviewed root execution removes only these two exact paths. If the
owner's execution output shows an unexpected consumer or path mismatch, stop and
retain that file rather than broadening deletion. The separate
`/opt/40zen/secrets/4scribe.env` is preserved.

### Tailscale Serve (delete only two paths)

Current read-only status on `40zen.tail055a00.ts.net:443`. Each handler value is
an object with a `Proxy` string, not a bare string:

```json
{"Handlers": {"/4scribe": {"Proxy": "http://127.0.0.1:8093"}}}
```

| Path | Target | Action |
| --- | --- | --- |
| `/` | `http://127.0.0.1:18789` | **KEEP** — OpenClaw/Afanasii Gateway, not STT |
| `/4scribe` | `http://127.0.0.1:8093` | **KEEP** — 4scribe WS/mobile/text ingress |
| `/parakeet-test` | `http://127.0.0.1:8091` | **DELETE** — legacy wrapper/playground/Android route |
| `/v1/audio/transcriptions/realtime` | `http://100.72.25.9:8090/v1/audio/transcriptions/realtime` | **DELETE** — legacy raw WS alias |

There is no root STT alias: `/` is the Gateway and must remain. No other TCP
Serve ports are configured. Removal uses per-path `off` commands only:

```bash
sudo tailscale serve --yes --https=443 --set-path=/parakeet-test off
sudo tailscale serve --yes --https=443 --set-path=/v1/audio/transcriptions/realtime off
```

**Never** use `tailscale serve reset`, global `serve off`, `serve clear`, or
`--https=443 off` for this cleanup. The script snapshots Serve JSON and proves
that every non-legacy handler is unchanged.

## Delete vs keep

| Resource | Decision | Rationale |
| --- | --- | --- |
| Legacy `parakeet` service stack, GGUFs, Compose backup | DELETE | Replaced by 4scribe; no runtime dependency after client acceptance |
| Legacy `parakeet-test` wrapper, playground, tests, code backups, local `.git` | DELETE | Owner authorized playground removal; Android/Loqui migrations replace wrapper |
| `parakeet-test` + `parakeet-stt` containers and `parakeet_default` network | DELETE | Obsolete runtime |
| Exact legacy images listed above | DELETE | No rollback copy retained by owner decision |
| Legacy `/parakeet-test` and raw WS Serve paths | DELETE | Replaced by `/4scribe`; old clients fail closed |
| `stt.env`, `parakeet-mobile.env` | DELETE only after primary usage confirmation | Dedicated legacy credentials; no reuse as 4scribe tokens |
| 4scribe container/image/compose/cache/`4scribe.env` | KEEP | Current accepted service |
| Tailscale `/`, `/4scribe`, unrelated routes/ports | KEEP | Gateway and current STT ingress |
| Shared Ollama, `frozenlab/qwen3-asr:1.7b`, `qwen3:0.6b`, embeddings | KEEP | 4scribe ASR/enhancement and unrelated consumers |
| `/opt/40zen/services/4scribe/model-cache/hf` aligner weights | KEEP | Current 4scribe runtime dependency |
| Repo `.venv`, `.dev-tools`, runtime paths | KEEP | Explicitly preserved development/runtime tooling |
| `NeMo-Speech.cpp` checkout (406 MB) | KEEP / out of scope | Build-source provenance, not a live runtime; separate owner decision if full source cleanup is wanted |
| Historical docs, research results, registry journal | KEEP as history | Do not rewrite history; update current routing inventory only at execution |
| Owner recordings / private replay copies | KEEP | Not touched, not opened |

## Client and consumer gate

| Consumer | Current contract | Acceptance |
| --- | --- | --- |
| HardAfanasii Android | Optional legacy provider used `/parakeet-test/api/mobile/transcribe?enhance=true`; 4scribe provider installed | **ACCEPTED 2026-10-21** — owner confirmed updated build installed/works; task `QUfX5DkfQlOp` Done |
| Loqui | Remote adapter internal ID `40zen-parakeet` now serves 4scribe; preserve it | **ACCEPTED 2026-10-21** — owner confirmed updated build installed/works; task `rJXb45dp2zWg` Done |
| Browser playground | `/parakeet-test/` temp UI | Owner authorized deletion; no replacement required |
| OpenClaw | Confirmed **not** a consumer of this STT | Do not launch broad OpenClaw exploration or read its configs/secrets |
| Standalone 4scribe text API / Loqui migration | Done and owner-accepted; public `/4scribe/v1/text/enhance` works after alias fix; two external clients remained 200 in latest accepted test | No new model/quality work in this task |
| Optional cloud Gemini / OpenClaw audio | Not migrated | Separate optional task; out of scope and not a blocker |

Client acceptance is satisfied. Local agents are still finalizing client
commits, but the installed builds were explicitly accepted by the owner. Server
deletion remains pending the owner root command.

## Execution plan (owner root execution pending)

`deployment/retire-legacy-stt.sh` implements this sequence. It remains safe and
unchanged; it is **not executed** by this preparation.

1. **Preflight (read-only):**
   - 4scribe container image exactly `sha256:f313…474`, running/healthy, restart
     count recorded, exactly one read-only bind
     `/opt/40zen/services/4scribe/model-cache/hf -> /models/hf`, `/healthz`
     reports `ok=true`, aligner `qwen3-forced-aligner-0.6b`, word timestamps.
   - Tailscale Serve matches the four current handlers and TCP 443 shape.
   - Legacy containers exist with expected names, Compose project labels, image
     IDs, mounts, host/default network, and 8090 port binding.
   - `parakeet_default` has only `parakeet-stt` attached.
   - Every exact legacy image tag exists and is used only by legacy containers.
   - Both secret paths exist, are regular non-symlink files, and the legacy-only
     usage check passed.
   - Directory targets are canonical, contain no symlink ancestor, and have no
     mountpoint beneath them.
2. **Remove the two Serve paths** with per-path `off`; compare Serve JSON against
   the snapshot to prove `/`, `/4scribe` and all unrelated handlers are intact.
3. **Stop/remove** `parakeet-test`, then `parakeet-stt`; remove the empty
   `parakeet_default` network.
4. **Remove exact image tags** listed in the script. No wildcard, prune, or
   broad image cleanup.
5. **Recheck and remove** `/opt/40zen/services/parakeet` and
   `/opt/40zen/services/parakeet-test` with `rm -rf --one-file-system`; remove
   the known `/tmp/opencode/parakeet-test` fixture and empty directory.
6. **Remove** `/opt/40zen/secrets/stt.env` and
   `/opt/40zen/secrets/parakeet-mobile.env` by exact path; no value is read.
7. **Post-check:** 4scribe same image, healthy, same restart count; `/healthz`
   OK; 8090/8091 listeners absent; 8093 listener present; legacy containers,
   network, image tags, directories, fixture and secret paths absent; Serve
   pre-minus-two-legacy-paths equals post, with `/` and `/4scribe` unchanged
   and any unrelated pre-existing handlers preserved.

Run modes:

```bash
# safe local preparation (already passed)
deployment/retire-legacy-stt.sh --check

# reviewed execution, root required
sudo env RETIRE_LEGACY_STT=YES \
  /opt/40zen/repos/4scribe/deployment/retire-legacy-stt.sh \
  --execute --clients-accepted
```

No old cleanup script may be used:

- `deployment/retire-host-runtime.sh` pins the obsolete 4scribe image
  `sha256:7708…` and is unrelated.
- `services/parakeet-test/Makefile` target `clean` uses global Serve operations;
  do not run it.
- `services/parakeet/scripts/smoke-test.sh` reads the legacy key and calls the
  old engine; do not run it as verification after deletion.

## Consequences

- `/parakeet-test` and `/v1/audio/transcriptions/realtime` stop resolving after
  the Serve paths are removed. Direct `100.72.25.9:8090` and loopback 8091
  disappear when the containers stop. Any un-migrated client fails closed; no
  automatic cloud fallback is introduced.
- The legacy engine/mobile bearer values are no longer accepted anywhere and
  the host-side files are removed. Client-stored copies are not remotely wiped;
  the client tasks must remove them. Neither value is reused as a 4scribe token.
- Browser playground is gone without replacement. Historical behavior remains
  in docs.
- 4scribe `/4scribe`, mobile and enhancement endpoints, its credential, model
  cache and `qwen3:0.6b` remain untouched.
- Deletion is irreversible. Per owner decision, no backup image, directory copy,
  model copy or rollback stack is retained. If a rollback requirement appears,
  stop before execution rather than creating a ceremonial backup.

## Metadata still needed from primary (bounded reads only)

`opencode` (uid 1001) is not in the `docker` group;
`/var/run/docker.sock` is `root:docker 0660`; non-interactive `sudo` is denied;
the secrets directory is unreadable. The following are therefore required
before the final image array and secret decision are frozen. Do not dump full
inspect output, `Config.Env`, process args, or secrets.

```bash
# Exact IDs, state and labels; no Config.Env.
docker inspect --format '{{.Name}} image={{.Image}} state={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} restarts={{.RestartCount}}' \
  parakeet-stt parakeet-test fourscribe-container

# Mounts/networks/ports/labels only.
docker inspect --format '{{.Name}} mounts={{json .Mounts}} networks={{json .NetworkSettings.Networks}} ports={{json .HostConfig.PortBindings}} project={{index .Config.Labels "com.docker.compose.project"}} service={{index .Config.Labels "com.docker.compose.service"}}' \
  parakeet-stt parakeet-test
docker inspect --format '{{.Name}} mounts={{json .Mounts}}' fourscribe-container

# Exact legacy tags/IDs/sizes.
docker image ls --format '{{.Repository}}:{{.Tag}} {{.ID}} {{.Size}}' |
  grep -E '^(parakeet-stt|parakeet-test|nemo-speech-runtime-cpu):'

# Network ownership/endpoints.
docker network ls --filter label=com.docker.compose.project=parakeet --format '{{.Name}} {{.ID}} {{.Driver}}'
docker network inspect parakeet_default --format '{{.Name}} {{len .Containers}} {{range .Containers}}{{.Name}} {{end}}'

# Secret path metadata only (no cat/grep values).
sudo stat -c '%n %s bytes mode=%a owner=%U group=%G' \
  /opt/40zen/secrets/stt.env /opt/40zen/secrets/parakeet-mobile.env
```

If the actual tags differ from `LEGACY_IMAGE_TAGS`, update that exact array and
re-review the script before execution. If any image has an additional tag, list
it explicitly; do not substitute a wildcard.

Reconcile the bounded legacy credential usage check after client acceptance:

```bash
grep -RIn --exclude-dir=.git --exclude-dir=__pycache__ \
  -E 'stt\.env|parakeet-mobile\.env' \
  /opt/40zen/services /opt/40zen/scripts /opt/40zen/docs /etc/systemd/system
```

Expected remaining references are historical docs/registry only. Any live
service/config reference means that secret file must stay and be reported.

## Documentation updates at EXECUTION (not now)

After successful execution, update current routing/consumer inventory in
`/opt/40zen/docs/stt.md`, `tailscale.md`, `architecture.md`, `services.md`,
`hard-afanasii.md` and `recovery.md` as applicable; mark the legacy runbook
historical rather than rewriting history. Append the outcome and verification
to `/opt/40zen/services/4scribe/RESOURCE-REGISTRY.md`. This preparation changes
only this plan, the bounded script, the repo allowlist and a local registry
append.

## Preparation tests

- `bash -n deployment/retire-legacy-stt.sh` — passed.
- `python3 -m unittest -v deployment/test_legacy_retirement.py` — 8 passed.
  Synthetic fixtures exercise Serve handler-object shape (`{"Proxy": ...}`),
  string-shape rejection, exact two-path removal, unrelated-route preservation,
  rejection of added/removed unrelated routes, remaining-legacy rejection, exact
  nine-tag inventory, and the corrected plan path.
- Read-only `tailscale serve status --json` parsed by
  `deployment/legacy_serve_guard.py pre` — passed against the actual handler
  object shape; no Serve mutation.
- Primary independently re-ran the 8 unit tests and `--check` against `9eb8ec3`
  and confirmed selected live Docker inventory and exact tags match.
- `deployment/retire-legacy-stt.sh --check` — passed; local paths,
  symlink/mount guards, Compose lines and 4scribe accepted-image guards verified.
- `--execute` without `RETIRE_LEGACY_STT=YES` — refused before any preflight.
- `--execute --clients-accepted` with confirmation but as non-root — refused
  before Docker/Tailscale access.
- No Docker mutation, container stop/restart, Serve change, secret read, model
  call, cloud call or production authenticated request was made.

## Post-execution documentation checklist (after root output)

1. Record the owner command output, timestamp, 4scribe image/health/restart
   count, and confirmation that no legacy resources remain.
2. Update current routing/consumer inventory in
   `/opt/40zen/docs/stt.md`, `tailscale.md`, `architecture.md`, `services.md`,
   `hard-afanasii.md` and `recovery.md`; keep historical notes as history.
3. Mark the old `services/parakeet` runbook historical; remove dead operational
   commands without rewriting research history.
4. Reconcile the local `RESOURCE-REGISTRY.md`: exact deleted paths/tags/routes,
   postchecks, secret-path removal, client acceptance references, and statement
   that no backup/rollback copy was created.
5. Verify final Serve state has `/`, `/4scribe` and any unrelated pre-existing
   handlers; no global reset. Verify 8090/8091 absent and 8093 healthy.
6. Publish the doc-only outcome and close task `NQf35XIvbfOf` only after primary
   accepts the root output and postchecks.

## Remaining execution items

1. Owner runs the reviewed root command:
   `sudo env RETIRE_LEGACY_STT=YES /opt/40zen/repos/4scribe/deployment/retire-legacy-stt.sh --execute --clients-accepted`.
2. Keep task `NQf35XIvbfOf` Doing until the root output and postchecks are
   reviewed.
3. Decide separately whether the non-runtime `NeMo-Speech.cpp` source checkout
   should also be removed; it is not part of this runtime retirement.
4. The known `/tmp/opencode/parakeet-test` fixture is included in the script.

**READY FOR OWNER EXECUTION** — clients accepted; no live retirement performed.
