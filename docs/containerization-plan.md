# 4scribe containerization plan (PLAN ONLY — no build/deploy)

Status: proposal for review. No image is built, no runtime/network/systemd/
Tailscale change is made, and no cutover is authorized by this document.
Review revision 2 fixes the network and drain ambiguities.

## 1. Goal and shape

One container for the 4scribe Python app plus its in-process Silero VAD and
Qwen3 forced aligner. **Ollama remains the existing external service**
(`frozenlab/qwen3-asr:1.7b`), not containerized and not reconfigured here.

Current accepted runtime on the host (ground truth for parity): Python 3.14.4,
torch 2.14.1+cpu, numpy 2.5.4, fastapi 0.143.3, uvicorn 0.54.0, httpx 0.28.1,
websockets 17.2, transformers 4.57.6, qwen-asr 0.0.6, packaging 26.3,
silero-vad 6.2.3. Aligner cache:
`/opt/40zen/services/4scribe/model-cache/hf` (1.8 GB, offline snapshot).

## 2. Image and dependencies

- **Base:** `python:3.14.4-slim` (match the working host) pinned by digest.
  Install CPU torch from the PyTorch CPU wheel index; no CUDA, no new framework.
- **Python parity gate:** if a conventional `3.12-slim` base is preferred, that
  is a separate verification gate: build the same pins in a throwaway 3.12 venv
  on the host, run the hermetic suite plus real Silero/aligner smoke, and only
  then accept 3.12 parity.
- **Pins:** fastapi, uvicorn, httpx, websockets, numpy, packaging,
  transformers, qwen-asr, silero-vad==6.2.3 (wheel SHA256 `7b7f5436…`),
  `torch==2.14.1+cpu` from the CPU index. Do not pin transient host-only
  packages the app does not import. `qwen-asr==0.0.6` is installed
  `--no-deps` to mirror the accepted host venv and avoid pulling the optional
  gradio/Flask UI tree; its real runtime deps are pinned explicitly.
- **Entrypoint:** `python -m uvicorn fourscribe.app:create_app --factory
  --app-dir src --host 127.0.0.1 --port <port>`; the port is passed explicitly
  per launch (see §6).
- **No baked weights/token/host IDs:** aligner weights are a read-only mount,
  never a layer. The 2.3 MB Silero JIT travels with the pinned wheel as a
  dependency artifact, not the aligner model. No `FSCRIBE_TOKEN` or host/tailnet
  identifier is baked.

## 3. Mounts, secrets, non-root

- Read-only model cache: host `.../model-cache/hf` → container `/models/hf:ro`.
- `FSCRIBE_ALIGNER_HF_HOME=/models/hf`, `HF_HUB_OFFLINE=1`,
  `TRANSFORMERS_OFFLINE=1`, `HF_HUB_DISABLE_TELEMETRY=1`.
- Bearer token via an external secret file (`EnvironmentFile` equivalent /
  `--env-file` from an owner-controlled path); never in the image or plan.
- Explicit non-root uid/gid matching the mounted cache; `--read-only` root
  filesystem plus a small writable `tmpfs` for `/tmp`; no-new-privileges.
  Because the service root is group-restricted on the host, the trial runs as
  the image uid with `--group-add` for the read-only cache's group (default
  `1000`); this grants read-only mount access, not host write.

## 4. CPU, memory and threads

Existing service parallelism: `FSCRIBE_INFERENCE_WORKERS=2`,
`FSCRIBE_ALIGNER_WORKERS=2`, one serialized Silero verification worker; host
torch default thread count 12 (the verifier restores it after load). Historical
in-process aligner evidence: bf16 CPU, peak RAM ~2.6 GB per decision doc.

- Initial CPU: grant the container the same core count the host torch uses
  (no quota reduction) so existing parallelism is preserved; no oversubscription.
- Initial memory: start uncapped or with a conservative cap above the measured
  two-stream peak; the historical ~2.6 GB is evidence, not a guarantee that
  “4 GB is enough”. Record the measured peak and set/retain the limit only
  after a two-stream benchmark.
- Verify effective torch/OMP/MKL thread counts inside the container; the Silero
  import sets torch threads to 1 and the app restores the previous value, so
  confirm this under the container before accepting. No scheduler change.

## 5. Health, readiness, drain (actual semantics)

- `/healthz`: liveness, aligner stack name, active `verifier` name. Does not
  load the aligner model.
- `/readyz`: Ollama `/api/tags` reachability only; not proof of decode
  readiness. Container healthcheck should use `/healthz`.
- **Drain is NOT a shutdown guarantee.** Today the client drives commit/ACK
  while the WebSocket is open; on server shutdown the app's `finally` sets
  `ended`, calls `session.close()` (marks closed, bumps generation, clears
  `commit_pending`) and cancels the outbound pump. In-flight finals that finish
  after `closed` return without emitting a final or a commit ACK. Therefore
  SIGTERM/uvicorn graceful shutdown must not be described as guaranteeing
  client commit/final ACK.
- Planned cutover/stop is **idle-only** (no active STT session), exactly like
  the host unit. The trial script fails closed if `ss` reports an active
  production 8093 connection, if port 18093 listens, or if the task container
  name already exists; cleanup removes only the container ID it created. Set an
  explicit container stop grace (for example `--stop-timeout`/`stop_grace_period`)
  and, if used, an explicit uvicorn `--timeout-graceful-shutdown`; treat both as
  bounded best-effort unwinding only, not as an ACK/flush contract. No shutdown
  redesign in this stage.
- Measurements: `cgroup memory.peak` is read inside the trial when readable;
  otherwise peak is explicitly unmeasured and a posthoc `docker stats`
  snapshot is labelled as such. Torch threads reported via `docker exec` are
  a default-probe (new process), not the running service's effective count.

## 6. Network, ports, trial and cutover gate

**Chosen design: Linux host networking, explicit port per launch.**

- In a bridge container, `127.0.0.1` is the container itself, so the host
  Ollama at `127.0.0.1:11434` is unreachable, and a container-local
  `127.0.0.1` uvicorn bind is not reachable through a published port. The host
  Ollama is loopback-only and must not be reconfigured or moved to `0.0.0.0`.
- Smallest explicit fit: run the trial container with **`--network=host`** (no
  port mapping; host networking is weaker isolation and shares the host
  loopback/port space). Uvicorn binds `127.0.0.1` and talks to Ollama at
  `127.0.0.1:11434` unchanged.
- **Port selection is explicit.** The current unit hardcodes
  `--port 8093`; `FSCRIBE_PORT` alone does not override the uvicorn CLI. Trial
  must pass `--port 18093` explicitly to keep the host unit’s 8093 untouched.
  (An env-driven entrypoint could remove the duplication later; not in scope.)
- **Trial smoke is authenticated loopback WS**, e.g.
  `ws://127.0.0.1:18093/v1/audio/transcriptions/realtime` (the app also answers
  under the `/4scribe` path). No new Tailscale route is designed or authorized;
  the existing Serve handler stays pointed at `127.0.0.1:8093`.
- Checks: `/healthz` shows `verifier=silero-vad-6.2.3`; short two-stream
  authenticated WS smoke; final/partial/ACK/word-timestamps behavior; no Ollama
  model download; no runtime egress.
- Owner gate before any cutover. Cutover (separately authorized): stop the host
  unit, run the container on `--port 8093`, keep the existing WSS route
  `/4scribe/v1/audio/transcriptions/realtime` unchanged. Exact rollback: stop
  the container and start the existing host unit.

## 7. What is explicitly not in scope

- No Dockerfile/Compose build, no image pull, no deploy, no systemd/Tailscale
  change in this stage.
- No Ollama changes/uninstall; Nemotron/parakeet/enhancer stay as-is.
- No rejected experimental behavior baked in (temporary lifecycle tracing is
  retired; energy-only ghost suppression is not the architecture).
- Accepted limitation remains: the old energy gate can still miss quiet speech;
  containerization does not change that. Loqui/HardAfanasii migrations are
  separate.

## 8. Open verification items before build

1. cp314 `torch==2.14.1+cpu` wheel exists in the chosen base and index.
2. Aligner loads offline from the read-only mount as the non-root container uid.
3. `--read-only` + tmpfs is compatible with transformers cache reads.
4. Effective torch/OMP/MKL thread counts and measured two-stream peak RSS under
   the chosen CPU/memory limits.
5. Exact two-stream authenticated-loopback smoke command and cutover/rollback
   owner gate.
