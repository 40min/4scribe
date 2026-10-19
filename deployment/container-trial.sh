#!/usr/bin/env bash
# 4scribe isolated container trial (owner-runnable).
# - builds the image from the service-root context (.dockerignore allowlist)
# - runs hermetic core tests INSIDE the image (no network)
# - runs the real app on loopback port 18093 with Linux host networking
#   (host Ollama 127.0.0.1:11434 stays unchanged; no port mapping)
# - runs authenticated loopback WS smokes with public RU/EN fixtures
# - measures peak memory and effective torch thread count (selected fields only)
# The production host unit on 127.0.0.1:8093 is never touched.
set -euo pipefail

ROOT=${ROOT:-/opt/40zen/services/4scribe}
IMAGE=${TRIAL_IMAGE:-4scribe-trial:local}
PORT=${TRIAL_PORT:-18093}
SCRATCH=${TRIAL_SCRATCH:-/tmp/opencode/4scribe-container-trial}
FIX=${TRIAL_FIXTURES:-/tmp/opencode/4scribe-vad-shadow/fixtures}
NAME="4scribe-trial-$PORT"
PY="$ROOT/.venv/bin/python"
TOKEN_FILE="$SCRATCH/trial.env"

mkdir -p "$SCRATCH"
chmod 700 "$SCRATCH"

# Task-only high-entropy token, 0600, never printed or passed on argv.
if [ ! -s "$TOKEN_FILE" ]; then
  umask 077
  "$PY" -c 'import secrets; print("FSCRIBE_TOKEN=" + secrets.token_urlsafe(32))' > "$TOKEN_FILE"
fi
chmod 600 "$TOKEN_FILE"

# Public fixtures were cleaned; recover tiny ones from the pinned manifest
# (max ~2 MB, no private audio/transcripts).
if [ ! -f "$FIX/en_normal_01.wav" ] || [ ! -f "$FIX/ru_normal_01.wav" ]; then
  if [ -x "$FIX/../fetch_fixtures.py" ] || [ -f "$FIX/../fetch_fixtures.py" ]; then
    (cd "$FIX/.." && "$PY" fetch_fixtures.py)
  else
    echo "BLOCKED: public fixtures absent and fetch_fixtures.py not found at $FIX/.." >&2
    exit 2
  fi
fi

cleanup() {
  docker stop "$NAME" >/dev/null 2>&1 || true
  docker rm -f "$NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "== build =="
docker build -t "$IMAGE" "$ROOT"

echo "== in-image hermetic core tests (no network) =="
docker run --rm --network=none --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=256m \
  --entrypoint python "$IMAGE" -m pytest tests -q -p no:cacheprovider \
    --ignore=tests/test_aligner_cache.py \
    --ignore=tests/test_diag_units.py \
    --ignore=tests/test_trace_replay.py

echo "== start trial on 127.0.0.1:$PORT (host network, read-only, non-root) =="
docker run -d --name "$NAME" \
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
  "$IMAGE" --port "$PORT"

for i in $(seq 1 60); do
  if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/healthz" > "$SCRATCH/healthz.json" 2>/dev/null; then
    break
  fi
  sleep 1
done
echo "healthz: $(cat "$SCRATCH/healthz.json")"

echo "== two-stream authenticated loopback WS smoke (RU + EN, public fixtures) =="
"$PY" "$ROOT/deployment/smoke_wss.py" \
  --url "ws://127.0.0.1:$PORT/v1/audio/transcriptions/realtime" \
  --token-file "$TOKEN_FILE" \
  --fixture "$FIX/ru_normal_01.wav" ru > "$SCRATCH/smoke_ru.log" 2>&1 &
RU_PID=$!
"$PY" "$ROOT/deployment/smoke_wss.py" \
  --url "ws://127.0.0.1:$PORT/v1/audio/transcriptions/realtime" \
  --token-file "$TOKEN_FILE" \
  --fixture "$FIX/en_normal_01.wav" en > "$SCRATCH/smoke_en.log" 2>&1 &
EN_PID=$!
wait "$RU_PID"; RU_RC=$?
wait "$EN_PID"; EN_RC=$?
cat "$SCRATCH/smoke_ru.log" "$SCRATCH/smoke_en.log"
echo "smoke_rc ru=$RU_RC en=$EN_RC"

echo "== selected measurements =="
docker stats --no-stream --format 'mem_usage={{.MemUsage}} cpu_percent={{.CPUPerc}}' "$NAME"
docker exec "$NAME" python -c 'import os, torch; print("torch_threads", torch.get_num_threads(), "omp", os.environ.get("OMP_NUM_THREADS"))'

cleanup
trap - EXIT
[ "$RU_RC" = 0 ] && [ "$EN_RC" = 0 ]
