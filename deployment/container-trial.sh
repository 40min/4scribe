#!/usr/bin/env bash
# 4scribe isolated container trial (owner-runnable as i40min; no sudo needed).
# - fail-closed preflight: docker access, disk budget, no active production
#   8093 connections, port 18093 free, task container name absent
# - builds the image, runs hermetic core tests INSIDE the image
# - starts the app on 127.0.0.1:18093 with Linux host networking (external
#   host Ollama at 127.0.0.1:11434; no port mapping)
# - fetches two pinned PUBLIC fixtures into this invocation's own mktemp dir
# - runs authenticated loopback WS smokes (RU and EN) and collects both codes
# - records cgroup memory.peak if readable, else labels a posthoc snapshot
# - stops/removes ONLY the container ID created by this invocation
set -euo pipefail

ROOT=${ROOT:-/opt/40zen/services/4scribe}
IMAGE=${TRIAL_IMAGE:-4scribe-trial:local}
PORT=${TRIAL_PORT:-18093}
KEEP_ALIVE=${TRIAL_KEEP_ALIVE_S:-60}
MODEL=${FSCRIBE_MODEL:-frozenlab/qwen3-asr:1.7b}
DISK_BUDGET_GB=${TRIAL_DISK_BUDGET_GB:-6}
PY="$ROOT/.venv/bin/python"

fail() { echo "FAIL: $*" >&2; exit 1; }

# --- 0. docker access + disk budget BEFORE any download/build -------------
command -v docker >/dev/null 2>&1 || fail "docker CLI not found"
docker info -f '{{.ServerVersion}}' >/dev/null 2>&1 || \
  fail "docker daemon not accessible to $(id -un); no bypass attempted"
command -v ss >/dev/null 2>&1 || fail "ss not found; cannot verify production state"
free_kb=$(df -Pk "$ROOT" | awk 'NR==2 {print $4}')
need_kb=$((DISK_BUDGET_GB * 1024 * 1024))
[ "${free_kb:-0}" -ge "$need_kb" ] || \
  fail "disk budget: free ${free_kb}KiB < ${DISK_BUDGET_GB}GiB required"
echo "trial_disk_budget_gb=$DISK_BUDGET_GB trial_disk_free_kb=$free_kb"

# --- 1. production safety preflight (fail closed) -------------------------
if [ -n "$(ss -H -tn state established "sport = :8093")" ]; then
  fail "active production :8093 connection(s); refusing to start trial"
fi
if [ -n "$(ss -H -ltn "sport = :$PORT")" ]; then
  fail "port $PORT is already listening; refusing to start trial"
fi
NAME="4scribe-trial-${PORT}-$(date +%s)-$$"
if [ -n "$(docker ps -aq --filter "name=^/${NAME}$")" ]; then
  fail "task container name $NAME already exists; refusing"
fi

# --- 2. own fresh scratch + receipts --------------------------------------
TRIAL_DIR=$(mktemp -d "${TMPDIR:-/tmp}/4scribe-container-trial.XXXXXX")
chmod 700 "$TRIAL_DIR"
FIX="$TRIAL_DIR/fixtures"
mkdir -p "$FIX"
TOKEN_FILE="$TRIAL_DIR/trial.env"
umask 077
"$PY" -c 'import secrets; print("FSCRIBE_TOKEN=" + secrets.token_urlsafe(32))' > "$TOKEN_FILE"
chmod 600 "$TOKEN_FILE"
echo "trial_dir=$TRIAL_DIR"

CID=""
RU_PID=""
EN_PID=""
cleanup() {
  [ -n "$RU_PID" ] && kill "$RU_PID" 2>/dev/null || true
  [ -n "$EN_PID" ] && kill "$EN_PID" 2>/dev/null || true
  if [ -n "$CID" ]; then
    # owned ID only; never touches a preexisting container
    docker stop "$CID" >/dev/null 2>&1 || true
    docker rm -f "$CID" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

# --- 3. reproducible public fixtures inside own scratch -------------------
"$PY" "$ROOT/deployment/fetch_trial_fixtures.py" --out "$FIX"

# --- 4. initial Ollama /api/ps metadata for THIS ASR only -----------------
PS_KNOWN=0
PRIOR_RESIDENT=unknown
if "$PY" - "$MODEL" > "$TRIAL_DIR/ollama_ps_before.txt" <<'PY'
import json, sys, urllib.request
model = sys.argv[1]
try:
    with urllib.request.urlopen("http://127.0.0.1:11434/api/ps", timeout=10) as r:
        d = json.load(r)
except Exception:
    print("ps_known=0")
    print("prior_resident=unknown")
    sys.exit(0)
models = [m for m in (d.get("models") or [])
          if (m.get("name") or m.get("model")) == model]
print("ps_known=1")
print("prior_resident=1" if models else "prior_resident=0")
print("asr_size_bytes=" + str(models[0].get("size") if models else 0))
PY
then :; else
  echo "ollama_ps=unavailable" >&2
fi
if [ -s "$TRIAL_DIR/ollama_ps_before.txt" ]; then
  PS_KNOWN=$(awk -F= '/^ps_known=/{print $2}' "$TRIAL_DIR/ollama_ps_before.txt")
  PRIOR_RESIDENT=$(awk -F= '/^prior_resident=/{print $2}' "$TRIAL_DIR/ollama_ps_before.txt")
  grep -E '^(ps_known|prior_resident|asr_size_bytes)=' "$TRIAL_DIR/ollama_ps_before.txt" || true
fi

# --- 5. build + in-image hermetic tests -----------------------------------
echo "== build =="
docker build -t "$IMAGE" "$ROOT"
echo "== in-image hermetic core tests (no network) =="
docker run --rm --network=none --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=256m \
  --entrypoint python "$IMAGE" -m pytest tests -q -p no:cacheprovider \
    --ignore=tests/test_aligner_cache.py \
    --ignore=tests/test_diag_units.py \
    --ignore=tests/test_trace_replay.py

# --- 6. isolated real trial ------------------------------------------------
echo "== start trial on 127.0.0.1:$PORT (host network, read-only, non-root) =="
CID=$(docker run -d --name "$NAME" \
  --network=host --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=512m \
  --user 10001:10001 \
  --group-add "${TRIAL_GROUP_ADD:-1000}" \
  --volume "$ROOT/model-cache/hf:/models/hf:ro" \
  --env-file "$TOKEN_FILE" \
  --env FSCRIBE_ALIGNER_HF_HOME=/models/hf \
  --env HF_HUB_OFFLINE=1 \
  --env TRANSFORMERS_OFFLINE=1 \
  --env HF_HUB_DISABLE_TELEMETRY=1 \
  --env FSCRIBE_KEEP_ALIVE_S="$KEEP_ALIVE" \
  "$IMAGE" --port "$PORT")
[ -n "$CID" ] || fail "docker run did not return a container id"

HEALTHY=0
for _ in $(seq 1 60); do
  if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/healthz" > "$TRIAL_DIR/healthz.json" 2>/dev/null; then
    HEALTHY=1
    break
  fi
  sleep 1
done
[ "$HEALTHY" = 1 ] || fail "trial never became healthy on :$PORT"
echo "healthz: $(cat "$TRIAL_DIR/healthz.json")"

echo "== two-stream authenticated loopback WS smoke (RU + EN) =="
RU_RC=1
EN_RC=1
set +e
"$PY" "$ROOT/deployment/smoke_wss.py" \
  --url "ws://127.0.0.1:$PORT/v1/audio/transcriptions/realtime" \
  --token-file "$TOKEN_FILE" --fixture "$FIX/ru_long_01.wav" --fixture ru \
  > "$TRIAL_DIR/smoke_ru.log" 2>&1 &
RU_PID=$!
"$PY" "$ROOT/deployment/smoke_wss.py" \
  --url "ws://127.0.0.1:$PORT/v1/audio/transcriptions/realtime" \
  --token-file "$TOKEN_FILE" --fixture "$FIX/en_normal_01.wav" --fixture en \
  --allow-no-deltas \
  > "$TRIAL_DIR/smoke_en.log" 2>&1 &
EN_PID=$!
wait "$RU_PID"; RU_RC=$?
wait "$EN_PID"; EN_RC=$?
set -e
RU_PID=""
EN_PID=""
echo "smoke_rc ru=$RU_RC en=$EN_RC"
echo "--- smoke_ru (last 30 lines) ---"; tail -n 30 "$TRIAL_DIR/smoke_ru.log" || true
echo "--- smoke_en (last 30 lines) ---"; tail -n 30 "$TRIAL_DIR/smoke_en.log" || true

# --- 7. selected measurements ---------------------------------------------
if PEAK=$(docker exec "$CID" cat /sys/fs/cgroup/memory.peak 2>/dev/null) && [ -n "$PEAK" ]; then
  echo "cgroup_memory_peak_bytes=$PEAK"
else
  echo "memory_peak=unmeasured (cgroup memory.peak not readable inside trial)"
fi
docker stats --no-stream --format \
  'memory_posthoc_snapshot={{.MemUsage}} cpu_posthoc_snapshot={{.CPUPerc}}' "$CID" || true
docker exec "$CID" python -c \
  'import os, torch; print("torch_threads_default_probe", torch.get_num_threads(), "omp_default_probe", os.environ.get("OMP_NUM_THREADS"))' \
  2>/dev/null || echo "torch_threads=probe_failed"

# --- 8. stop owned container, then conditional unload ---------------------
docker stop "$CID" >/dev/null 2>&1 || true
docker rm -f "$CID" >/dev/null 2>&1 || true
CID=""

if [ -n "$(ss -H -tn state established "sport = :8093")" ]; then
  echo "ollama_unload=skipped reason=production_8093_active"
elif [ "$PS_KNOWN" = 1 ] && [ "$PRIOR_RESIDENT" = 0 ]; then
  if curl -fsS --max-time 15 -X POST http://127.0.0.1:11434/api/generate \
       -H 'Content-Type: application/json' \
       -d "{\"model\":\"$MODEL\",\"keep_alive\":0}" > /dev/null; then
    echo "ollama_unload=attempted model=$MODEL (was not resident before trial)"
  else
    echo "ollama_unload=attempt_failed model=$MODEL"
  fi
else
  echo "ollama_unload=skipped ps_known=$PS_KNOWN prior_resident=$PRIOR_RESIDENT"
fi

# --- 9. verdict -----------------------------------------------------------
[ "$RU_RC" = 0 ] && [ "$EN_RC" = 0 ] || fail "smoke failure(s): ru=$RU_RC en=$EN_RC"
echo "trial_receipts=$TRIAL_DIR"
