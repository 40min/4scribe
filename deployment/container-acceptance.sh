#!/usr/bin/env bash
# Minimal disposable-container acceptance for the 4scribe mobile batch API.
#
# Run as the account that can use Docker (primary/owner). This does NOT touch
# the production container, image, compose file, routes or secrets.
#
# What it does:
#   1. builds the CURRENT working tree into a fresh tag (never reuses
#      `4scribe:dev-mobile`, which predates the final source/tests);
#   2. runs the hermetic image test suite (no Ollama/model/weights) with the
#      real ffmpeg installed by the Dockerfile;
#   3. starts a disposable read-only container on 127.0.0.1:18095 with fake
#      tokens, aligner and Silero disabled, and checks health + auth isolation.
#
# Optional real-Ollama batch smoke is documented in docs/container-acceptance.md;
# it is deliberately not part of this script so acceptance cannot depend on or
# disturb the production model service.
set -euo pipefail

cd "$(dirname "$0")/.."
REPO=$PWD
SHORT=$(git rev-parse --short HEAD)
IMAGE=${IMAGE:-4scribe:dev-acceptance-$SHORT}
PORT=${PORT:-18095}
NAME=${NAME:-4scribe-acceptance-$$}
RT_TOKEN=acceptance-realtime-0000000000
MOB_TOKEN=acceptance-mobile-0000000000

if [ -n "$(git status --porcelain)" ]; then
  echo "NOTE: building the working tree, which has uncommitted changes:" >&2
  git status --short >&2
fi

cleanup() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "== build $IMAGE from $REPO (HEAD $SHORT) =="
docker build -t "$IMAGE" .

echo "== image prerequisites =="
docker run --rm --entrypoint ffmpeg "$IMAGE" -version | head -1
docker run --rm --entrypoint python "$IMAGE" -c 'import python_multipart, fastapi, torch; print("python-multipart/fastapi/torch ok")'

echo "== hermetic image tests (no model, no network) =="
# test_aligner_cache needs the host read-only weight snapshot; test_diag_units
# is a local-only harness that is intentionally not in the image.
docker run --rm --network none --entrypoint python "$IMAGE" \
  -m pytest tests -q -p no:cacheprovider \
  --ignore=tests/test_aligner_cache.py \
  --ignore=tests/test_diag_units.py

echo "== disposable health/auth smoke on 127.0.0.1:$PORT =="
docker run -d --name "$NAME" --network host --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=128m \
  --security-opt no-new-privileges:true \
  -e FSCRIBE_TOKEN="$RT_TOKEN" \
  -e FSCRIBE_MOBILE_TOKEN="$MOB_TOKEN" \
  -e FSCRIBE_ALIGNER_ENABLED=0 \
  -e FSCRIBE_SILERO_ENABLED=0 \
  "$IMAGE" --port "$PORT" >/dev/null

ready=0
for _ in $(seq 1 60); do
  if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/healthz" >/tmp/4scribe-accept-health.json; then
    ready=1
    break
  fi
  sleep 1
done
if [ "$ready" -ne 1 ]; then
  echo "FAIL: container did not become healthy" >&2
  docker logs "$NAME" >&2
  exit 1
fi
cat /tmp/4scribe-accept-health.json; echo

code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
UNAUTH=$(code -X POST "http://127.0.0.1:$PORT/4scribe/v1/audio/transcriptions")
WSAUTH=$(code -X POST -H "Authorization: Bearer $RT_TOKEN" \
  "http://127.0.0.1:$PORT/4scribe/v1/audio/transcriptions")
MOBAUTH=$(code -X POST -H "Authorization: Bearer $MOB_TOKEN" \
  "http://127.0.0.1:$PORT/4scribe/v1/audio/transcriptions")

printf 'unauthenticated=%s realtime-token=%s mobile-token-empty-body=%s\n' \
  "$UNAUTH" "$WSAUTH" "$MOBAUTH"
[ "$UNAUTH" = 401 ] || { echo "FAIL: unauthenticated not 401" >&2; exit 1; }
[ "$WSAUTH" = 401 ] || { echo "FAIL: realtime token not 401" >&2; exit 1; }
# Authenticated but body-less: 400/413 proves the mobile token passed auth,
# without any model or ffmpeg work.
[ "$MOBAUTH" = 400 ] || [ "$MOBAUTH" = 413 ] || {
  echo "FAIL: mobile token not accepted (got $MOBAUTH)" >&2; exit 1; }

echo "OK: container build, ffmpeg, hermetic tests, health and auth isolation"
echo "Disposable container removed. Image left for review: $IMAGE"
