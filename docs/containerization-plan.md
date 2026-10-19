# 4scribe containerization plan (PLAN ONLY — no build/deploy)

Status: proposal for review. No image is built, no runtime/network/systemd/
Tailscale change is made, and no cutover is authorized by this document.

## 1. Goal and shape

One container for the 4scribe Python app plus its in-process Silero VAD and
Qwen3 forced aligner. **Ollama remains the existing external service**
(`http://127.0.0.1:11434`, model `frozenlab/qwen3-asr:1.7b`) and is not
containerized here. No ASR weight is baked into the image.

Current accepted runtime on the host (ground truth for parity): Python 3.14.4,
torch 2.14.1+cpu, numpy 2.5.4, fastapi 0.143.3, uvicorn 0.54.0, httpx 0.28.1,
websockets 17.2, transformers 4.57.6, qwen-asr 0.0.6, packaging 26.3,
silero-vad 6.2.3. The aligner cache is
`/opt/40zen/services/4scribe/model-cache/hf` (1.8 GB, offline snapshot).

## 2. Image and dependencies

- **Base:** `python:3.14.4-slim` (match the working host) pinned by digest.
  Install CPU torch from the PyTorch CPU wheel index; no CUDA, no new framework.
- **Python parity gate:** if the owner prefers a conventional `3.12-slim` base,
  that is a separate gate, not an assumption: build the same pinned deps in a
  throwaway 3.12 venv on the host, run the hermetic suite plus real Silero and
  aligner smoke, and only then accept 3.12 parity.
- **Pins:** fastapi, uvicorn, httpx, websockets, numpy, packaging,
  transformers, qwen-asr, silero-vad==6.2.3 (wheel SHA256
  `7b7f5436…`). `torch==2.14.1+cpu` from the CPU index. Do not pin transient
  host-only packages that the app does not import.
- **Entrypoint:** `python -m uvicorn fourscribe.app:create_app --factory
  --app-dir src --host 127.0.0.1 --port 8093` (same command as the host unit).
- **No baked weights/token/host IDs:** aligner weights are a read-only mount,
  never copied into a layer. The 2.3 MB Silero JIT travels with the pinned
  wheel; it is a dependency artifact, not the aligner model. No `FSCRIBE_TOKEN`
  or host/tailnet identifier is baked.

## 3. Mounts, secrets, non-root

- Read-only model cache: host `.../model-cache/hf` → container `/models/hf:ro`.
- `FSCRIBE_ALIGNER_HF_HOME=/models/hf`, `HF_HUB_OFFLINE=1`,
  `TRANSFORMERS_OFFLINE=1`, `HF_HUB_DISABLE_TELEMETRY=1`.
- Bearer token via an external secret file (`EnvironmentFile` equivalent /
  `--env-file` from an owner-controlled path); never in the image or plan.
- Run as an explicit non-root uid/gid matching the mounted cache; `--read-only`
  root filesystem plus a small writable `tmpfs` for `/tmp`; no-new-privileges.
  The app already writes nothing except optional trace output, which is not
  baked in.

## 4. CPU, memory and threads

Historical in-process aligner evidence: bf16 CPU, peak RAM ~2.6 GB per
decision doc; Silero adds a ~2.3 MB JIT model and a single serialized
verification worker. The plan therefore reserves ≥4 GB and a CPU quota rather
than unbounded host CPU. Set `OMP_NUM_THREADS`/`MKL_NUM_THREADS` explicitly to
avoid oversubscription; note the Silero import sets torch threads to 1 and the
app restores the previous value, so verify the effective value inside the
container before accepting. No scheduler/framework change.

## 5. Health, readiness, drain

- `/healthz`: liveness, aligner stack name, active `verifier` name. Does not
  load the aligner model.
- `/readyz`: Ollama `/api/tags` reachability only; not proof of decode
  readiness. Container healthcheck should use `/healthz`; readiness remains a
  diagnostic, not a load-balancer gate.
- Stop/drain: SIGTERM → uvicorn graceful shutdown with a bounded grace period
  (start at 30 s) so in-flight finals and commit ACKs finish; preserve
  finals-before-ACK. Never stop/restart while a recording is active.

## 6. Ports, trial and cutover gate

- Existing host unit owns `127.0.0.1:8093`; do not collide. **Trial the
  container on an isolated loopback port (for example 18093)** with the
  host unit still running.
- Trial checks: `/healthz` shows `verifier=silero-vad-6.2.3`; short
  two-stream authenticated WSS smoke; final/partial/ACK/word-timestamps
  behavior; no Ollama model download; no network egress at runtime.
- Owner gate before any cutover. Cutover (later, separately authorized):
  stop the host unit, bind the container to 8093, keep the existing WSS route
  `/4scribe/v1/audio/transcriptions/realtime` unchanged. Exact rollback is to
  stop the container and start the existing host unit again.

## 7. What is explicitly not in scope

- No Dockerfile/Compose build, no image pull, no deploy, no systemd/Tailscale
  change in this stage.
- No Ollama model changes/uninstall; Nemotron/parakeet/enhancer stay as-is.
- No rejected experimental behavior baked in: the temporary lifecycle-trace
  drop-in is retired; energy-only ghost suppression is not the architecture.
- Accepted limitation remains: the old energy gate can still miss quiet speech;
  containerization does not change that. Loqui/HardAfanasii migrations are
  separate.

## 8. Open verification items before build

1. cp314 `torch==2.14.1+cpu` wheel exists in the chosen base and index.
2. Aligner loads offline from the read-only mount as the non-root container uid.
3. Docker/OCI `--read-only` + tmpfs is compatible with transformers cache reads.
4. Effective torch thread count and aligner peak RSS under the chosen limits.
5. Exact two-stream smoke command and cutover/rollback owner gate.
