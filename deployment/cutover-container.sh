#!/usr/bin/env bash
# 4scribe permanent container cutover (OWNER/ROOT ONLY; idle-only).
#
# Switches the loopback 8093 listener from fourscribe.service to the accepted
# container image, without touching Tailscale Serve, the token value, Ollama,
# Loqui, the host venv, the model cache or any foreign container.
#
# The accepted image is identified by immutable ID, not by a mutable trial tag.
# A persistent local tag is created/reused only after the ID is verified. The
# running container's image ID is checked again before success is reported.
#
# On any failure after the host unit is stopped, this script stops only the
# container carrying our ownership label and restores the previous host unit
# enabled/active state. SIGTERM is bounded best-effort unwinding, NOT a final-ACK
# guarantee; therefore cutover refuses if any 8093 session is established and
# fails closed if that cannot be determined.
set -euo pipefail

ROOT=${ROOT:-/opt/40zen/services/4scribe}
COMPOSE="$ROOT/deployment/compose.yaml"
PROJECT=fourscribe
SERVICE=fourscribe
CNAME=fourscribe-container
IMAGE_TAG=4scribe:accepted-2026-10-19
EXPECTED_IMAGE_ID=sha256:7708a83bff51186ec273612b63fe6e6575fb46971e49e74a5c226610791e6517
BASE_COMMIT=948073011112e3cd164ca8269388bc709e1cfa69
HOST_UNIT=fourscribe.service
PORT=8093
SECRET_FILE=/opt/40zen/secrets/4scribe.env
HF_CACHE="$ROOT/model-cache/hf"
GRACE=20
PYBIN="$ROOT/.venv/bin/python"

fail() { echo "FAIL: $*" >&2; exit 1; }
note() { echo "$*"; }

[ "$(id -u)" -eq 0 ] || fail "run as root/owner (systemd + root-owned env file required)"

for cmd in docker curl ss git systemctl stat find; do
  command -v "$cmd" >/dev/null 2>&1 || fail "$cmd not found"
done
[ -x "$PYBIN" ] || PYBIN=$(command -v python3 || true)
[ -n "$PYBIN" ] || fail "no python3 for bounded health parsing"
docker info -f '{{.ServerVersion}}' >/dev/null 2>&1 || fail "docker daemon not accessible"
docker compose version >/dev/null 2>&1 || fail "docker compose v2 not available"
[ -f "$COMPOSE" ] || fail "compose file missing: $COMPOSE"
[ -f "$SECRET_FILE" ] || fail "root-owned secret env file missing (not read/printed)"
[ -d "$HF_CACHE" ] || fail "model cache missing: $HF_CACHE"

TMP=$(mktemp -d "${TMPDIR:-/tmp}/4scribe-cutover.XXXXXX")
chmod 700 "$TMP"
cleanup() { if [ -n "${TMP:-}" ]; then rm -rf "$TMP"; fi; }
trap cleanup EXIT

HOST_WAS_ACTIVE=$(systemctl is-active "$HOST_UNIT" 2>/dev/null || true)
HOST_WAS_ENABLED=$(systemctl is-enabled "$HOST_UNIT" 2>/dev/null || true)
ROLLBACK_ARMED=0
ROLLBACK_RUNNING=0
CID=""

owned_cid() {
  local id=$1 svc
  svc=$(docker inspect -f '{{index .Config.Labels "com.40zen.service"}}' "$id" 2>/dev/null || true)
  [ "$svc" = "4scribe" ]
}

check_idle() {
  local out
  if ! out=$(ss -H -tn state established 'sport = :8093' 2>/dev/null); then
    fail "cannot determine 8093 session state (ss failed); idle-only cutover refused"
  fi
  [ -z "$out" ] || fail "active 8093 session(s) present; idle-only cutover refused"
}

health_ok() {
  curl -fsS --max-time 3 "http://127.0.0.1:$PORT/healthz" -o "$TMP/healthz.json" 2>/dev/null || return 1
  "$PYBIN" - "$TMP/healthz.json" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    sys.exit(1)
ok = (
    d.get("ok") is True
    and d.get("aligner") == "qwen3-forced-aligner-0.6b"
    and d.get("verifier") == "silero-vad-6.2.3"
    and d.get("word_timestamps") is True
)
sys.exit(0 if ok else 1)
PY
}

rollback() {
  local why=$1 ids id ok=0 left=0
  [ "$ROLLBACK_RUNNING" = 0 ] || exit 1
  ROLLBACK_RUNNING=1
  trap - ERR
  echo "ROLLBACK: $why" >&2
  ids=$(docker compose -f "$COMPOSE" -p "$PROJECT" ps -a -q 2>/dev/null || true)
  if [ -n "${CID:-}" ]; then
    ids="$ids $CID"
  fi
  for id in $ids; do
    [ -n "$id" ] || continue
    if owned_cid "$id"; then
      if docker stop -t "$GRACE" "$id" >/dev/null 2>&1; then
        echo "rollback_stopped_owned_container=$id" >&2
      else
        echo "rollback_warning=stop_failed id=$id" >&2
      fi
    else
      echo "rollback_warning=container_not_owned_not_stopped id=$id" >&2
    fi
  done
  left=$(docker ps -q --filter "name=^/${CNAME}$" 2>/dev/null || true)
  if [ -n "$left" ] && owned_cid "$left"; then
    echo "rollback_warning=owned_container_still_running id=$left" >&2
    left=1
  else
    left=0
  fi
  if [ "$HOST_WAS_ACTIVE" = "active" ]; then
    case "$HOST_WAS_ENABLED" in
      enabled|enabled-runtime) systemctl enable "$HOST_UNIT" >/dev/null 2>&1 || true ;;
    esac
    systemctl start "$HOST_UNIT" >/dev/null 2>&1 || true
  fi
  for _ in $(seq 1 60); do
    if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
      ok=1
      break
    fi
    sleep 1
  done
  if [ "$ok" = 1 ] && [ "$left" = 0 ] && [ "$(systemctl is-active "$HOST_UNIT" 2>/dev/null || true)" = "active" ]; then
    echo "rollback_result=host_unit_active host_healthz=ok" >&2
  else
    echo "rollback_result=FAILED host_healthz=$ok container_still_running=$left host_unit_active=$(systemctl is-active "$HOST_UNIT" 2>/dev/null || true); inspect: systemctl status $HOST_UNIT" >&2
  fi
  exit 1
}

on_error() {
  local rc=$?
  trap - ERR
  if [ "$ROLLBACK_ARMED" = 1 ]; then
    rollback "unexpected failure rc=$rc"
  else
    exit "$rc"
  fi
}
trap on_error ERR

note "== preflight: git/image/config/permissions =="
git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1 || fail "not a git repository: $ROOT"
git -C "$ROOT" cat-file -e "$BASE_COMMIT^{commit}" 2>/dev/null || fail "accepted base commit missing"
git -C "$ROOT" merge-base --is-ancestor "$BASE_COMMIT" HEAD || fail "accepted base commit is not an ancestor of HEAD"
if ! git -C "$ROOT" diff --quiet "$BASE_COMMIT" HEAD -- src Dockerfile requirements-container.txt .dockerignore; then
  fail "runtime/build inputs differ from accepted commit $BASE_COMMIT"
fi
if [ -n "$(git -C "$ROOT" status --porcelain --untracked-files=no -- src Dockerfile requirements-container.txt .dockerignore)" ]; then
  fail "runtime/build inputs have uncommitted changes"
fi
note "git_head=$(git -C "$ROOT" rev-parse --short HEAD) base_commit=${BASE_COMMIT:0:7}"

docker image inspect "$EXPECTED_IMAGE_ID" >/dev/null 2>&1 || \
  fail "accepted image ID is absent locally; do not rebuild blindly or upload to a registry"
TAG_ID=$(docker image inspect -f '{{.Id}}' "$IMAGE_TAG" 2>/dev/null || true)
if [ -z "$TAG_ID" ]; then
  docker tag "$EXPECTED_IMAGE_ID" "$IMAGE_TAG" || fail "cannot create persistent local image tag"
  TAG_ID=$(docker image inspect -f '{{.Id}}' "$IMAGE_TAG")
  note "image_tag_created=$IMAGE_TAG"
fi
[ "$TAG_ID" = "$EXPECTED_IMAGE_ID" ] || fail "persistent tag $IMAGE_TAG does not resolve to the accepted image ID"
note "image_id=$TAG_ID image_tag=$IMAGE_TAG"

PROBE=$(find "$HF_CACHE/hub" -type f -name config.json -print -quit 2>/dev/null || true)
[ -n "$PROBE" ] || fail "no aligner config.json found under $HF_CACHE/hub"
check_model_path() {
  local p=$1 gid mode
  gid=$(stat -c '%g' "$p")
  mode=$((8#$(stat -c '%a' "$p")))
  [ "$gid" = 1000 ] || fail "model cache path group gid=$gid (expected 1000): $p"
  if [ -d "$p" ]; then
    [ $((mode & 050)) -eq 050 ] || fail "model cache directory lacks group r-x: $p"
  else
    [ $((mode & 040)) -eq 040 ] || fail "model cache file lacks group read: $p"
  fi
}
check_model_path "$HF_CACHE"
PARENT=$(dirname "$PROBE")
while [ "$PARENT" != "$HF_CACHE" ]; do
  check_model_path "$PARENT"
  PARENT=$(dirname "$PARENT")
done
check_model_path "$PROBE"
if command -v setpriv >/dev/null 2>&1; then
  if setpriv --reuid=10001 --regid=10001 --groups=1000 -- test -r "$PROBE" 2>/dev/null; then
    note "model_read_probe=pass uid=10001 gid=1000"
  else
    note "model_read_probe=unavailable (static gid/mode checks passed)"
  fi
fi

docker compose -f "$COMPOSE" -p "$PROJECT" config -q >/dev/null 2>&1 || \
  fail "compose config validation failed (no interpolated secret output shown)"

DOCKER_BOOT=0
for u in docker.service docker.socket; do
  case "$(systemctl is-enabled "$u" 2>/dev/null || true)" in
    enabled|enabled-runtime) DOCKER_BOOT=1 ;;
  esac
done
[ "$DOCKER_BOOT" = 1 ] || fail "Docker is not enabled at boot; restart:unless-stopped would not survive reboot"
note "docker_boot=enabled"

EXISTING=$(docker ps -aq --filter "name=^/${CNAME}$" 2>/dev/null || true)
if [ -n "$EXISTING" ]; then
  owned_cid "$EXISTING" || fail "foreign container named $CNAME; refusing to touch it"
  EXISTING_IMAGE=$(docker inspect -f '{{.Image}}' "$EXISTING")
  [ "$EXISTING_IMAGE" = "$EXPECTED_IMAGE_ID" ] || fail "owned container has unexpected image ID"
  if [ "$(docker inspect -f '{{.State.Running}}' "$EXISTING")" = "true" ]; then
    [ "$HOST_WAS_ACTIVE" != "active" ] || fail "both host unit and container report active; refusing ambiguous state"
    case "$HOST_WAS_ENABLED" in
      enabled|enabled-runtime) systemctl disable "$HOST_UNIT" >/dev/null 2>&1 || fail "cannot disable host unit for boot safety" ;;
    esac
    health_ok || fail "running container is not healthy at /healthz"
    note "cutover_result=already_active container_id=$EXISTING"
    note "host_unit=inactive/disabled healthz=$(cat "$TMP/healthz.json")"
    exit 0
  fi
fi

[ "$HOST_WAS_ACTIVE" = "active" ] || fail "host unit is not active ($HOST_WAS_ACTIVE); refusing non-baseline cutover"
case "$HOST_WAS_ENABLED" in
  enabled|enabled-runtime) ;;
  *) fail "host unit is not enabled ($HOST_WAS_ENABLED); refusing non-baseline cutover" ;;
esac
check_idle

note "== switch: disable/stop host unit, start accepted container =="
check_idle
ROLLBACK_ARMED=1
systemctl disable --now "$HOST_UNIT" >/dev/null 2>&1 || rollback "failed to disable/stop $HOST_UNIT"

FREE=0
for _ in $(seq 1 20); do
  OUT=$(ss -H -ltn 'sport = :8093') || rollback "ss failed while waiting for port release"
  if [ -z "$OUT" ]; then FREE=1; break; fi
  sleep 1
done
[ "$FREE" = 1 ] || rollback "port 8093 still listening after host unit stop"

docker compose -f "$COMPOSE" -p "$PROJECT" up -d --force-recreate >"$TMP/compose-up.log" 2>&1 || {
  tail -n 20 "$TMP/compose-up.log" >&2 || true
  rollback "docker compose up failed"
}
CID=$(docker compose -f "$COMPOSE" -p "$PROJECT" ps -q "$SERVICE" 2>/dev/null | head -n 1)
[ -n "$CID" ] || rollback "compose did not report a container id"
owned_cid "$CID" || rollback "started container lacks com.40zen.service=4scribe label"
IMG=$(docker inspect -f '{{.Image}}' "$CID")
[ "$IMG" = "$EXPECTED_IMAGE_ID" ] || rollback "running container image ID mismatch ($IMG)"

HEALTHY=0
for _ in $(seq 1 90); do
  if health_ok; then HEALTHY=1; break; fi
  sleep 1
done
[ "$HEALTHY" = 1 ] || rollback "bounded startup health/verifier check failed"

OUT=$(ss -H -ltn 'sport = :8093') || rollback "ss failed after container start"
[ -n "$OUT" ] || rollback "no listener on 127.0.0.1:8093 after container start"
[ "$(systemctl is-active "$HOST_UNIT" 2>/dev/null || true)" != "active" ] || rollback "host unit unexpectedly active"
case "$(systemctl is-enabled "$HOST_UNIT" 2>/dev/null || true)" in
  enabled|enabled-runtime) rollback "host unit still enabled; boot duplication risk" ;;
esac

ROLLBACK_ARMED=0
trap - ERR
note "cutover_result=active"
note "container_id=$CID image_id=$IMG"
note "healthz=$(cat "$TMP/healthz.json")"
note "host_unit=inactive/disabled tailscale_route=unchanged"
note "readiness_scope=startup_health_only; real aligner/decode smoke was accepted on the same immutable image; /readyz is not full readiness"
note "rollback_command=sudo $ROOT/deployment/rollback-container.sh"
