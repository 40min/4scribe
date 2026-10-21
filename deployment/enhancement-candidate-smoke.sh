#!/usr/bin/env bash
# Disposable pre-promotion smoke for the accepted enhancement candidate.
# Uses ONLY fake tokens, no production env/secret, no model call.
# Does not build, does not touch production container/compose/routes.
#
# Run as a Docker-capable account (primary/owner) BEFORE promotion:
#   ./deployment/enhancement-candidate-smoke.sh
set -euo pipefail

IMAGE=${IMAGE:-4scribe:dev-acceptance-7717021}
PORT=${PORT:-18096}
NAME=${NAME:-4scribe-enh-smoke-$$}
RT=acceptance-realtime-0000000000
MOB=acceptance-mobile-0000000000
ENH=acceptance-enhancement-0000000000
EXPECTED_IDS="sha256:006589f5e1a3c62363a8bd34e55d8f2be6e7ba90d1bd065a016ff4bfbdf7eac4 sha256:2f8903baf787e78e24778a01e16943bf10286b71c1cb3f0e75e07ea8f1e306c3"

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

if [ -n "$(ss -Hltn "( sport = :$PORT )" 2>/dev/null)" ]; then
  echo "FAIL: port $PORT already has a listener" >&2
  exit 1
fi

actual=$(docker image inspect --format '{{.Id}}' "$IMAGE")
found=0
for id in $EXPECTED_IDS; do [ "$actual" = "$id" ] && found=1; done
if [ "$found" -ne 1 ]; then
  echo "FAIL: candidate image ID does not match accepted build evidence" >&2
  exit 1
fi

docker run -d --name "$NAME" --network host --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=128m \
  --security-opt no-new-privileges:true \
  -e FSCRIBE_TOKEN="$RT" \
  -e FSCRIBE_MOBILE_TOKEN="$MOB" \
  -e FSCRIBE_ENHANCEMENT_TOKEN="$ENH" \
  -e FSCRIBE_ALIGNER_ENABLED=0 \
  -e FSCRIBE_SILERO_ENABLED=0 \
  "$IMAGE" --port "$PORT" >/dev/null

health=/tmp/4scribe-enh-smoke-health.json
ready=0
for _ in $(seq 1 60); do
  if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/healthz" >"$health"; then
    ready=1; break
  fi
  sleep 1
done
if [ "$ready" -ne 1 ]; then
  echo "FAIL: candidate container did not become healthy" >&2
  docker logs "$NAME" >&2
  exit 1
fi

python3 - "$health" <<'PY'
import json, sys
h = json.load(open(sys.argv[1]))
te = h.get("text_enhancement") or {}
assert te.get("enabled") is True, h
assert te.get("reason") == "ok", h
assert te.get("completion_unknown") is False, h
assert h.get("mobile_batch") == {"enabled": True, "reason": "ok"}, h
PY

code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
base="http://127.0.0.1:$PORT/4scribe/v1/text/enhance"
NOAUTH=$(code -X POST -H 'Content-Type: application/json' -d '{"text":""}' "$base")
WSAUTH=$(code -X POST -H "Authorization: Bearer $RT" -H 'Content-Type: application/json' -d '{"text":""}' "$base")
MOBAUTH=$(code -X POST -H "Authorization: Bearer $MOB" -H 'Content-Type: application/json' -d '{"text":""}' "$base")
ENHBODY=$(curl -s -X POST -H "Authorization: Bearer $ENH" -H 'Content-Type: application/json' -d '{"text":""}' "$base")
BATCH=$(code -X POST -H "Authorization: Bearer $MOB" \
  "http://127.0.0.1:$PORT/4scribe/v1/audio/transcriptions")

printf 'enhance-noauth=%s ws-token=%s mobile-token=%s enh-invalid=%s mobile-batch=%s\n' \
  "$NOAUTH" "$WSAUTH" "$MOBAUTH" "$ENHBODY" "$BATCH"
[ "$NOAUTH" = 401 ] || { echo "FAIL: enhancement no-auth not 401" >&2; exit 1; }
[ "$WSAUTH" = 401 ] || { echo "FAIL: WS token accepted by enhancement" >&2; exit 1; }
[ "$MOBAUTH" = 401 ] || { echo "FAIL: mobile token accepted by enhancement" >&2; exit 1; }
[ "$ENHBODY" = '{"error":{"code":"invalid_input"}}' ] || {
  echo "FAIL: enhancement token/invalid payload check failed" >&2; exit 1; }
[ "$BATCH" = 400 ] || [ "$BATCH" = 413 ] || {
  echo "FAIL: existing mobile token no longer passes auth" >&2; exit 1; }

echo "OK: candidate enhancement health/auth isolation (fake tokens, no model)"
echo "Disposable container removed; image left unchanged: $IMAGE"
