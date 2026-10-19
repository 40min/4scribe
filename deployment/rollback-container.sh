#!/usr/bin/env bash
# 4scribe container rollback (OWNER/ROOT ONLY; idle-only).
#
# Stops only the container carrying the 4scribe ownership label, then restores
# the previous host systemd service. The container is stopped and left in place
# for logs/inspection; it is not removed. The accepted image tag, host venv,
# model cache and root-owned token file are never removed or read.
#
# SIGTERM is bounded best-effort unwinding, NOT a final-ACK guarantee. This
# script refuses if any 8093 session is established and fails closed if that
# state cannot be determined.
set -euo pipefail

ROOT=${ROOT:-/opt/40zen/services/4scribe}
COMPOSE="$ROOT/deployment/compose.yaml"
PROJECT=fourscribe
CNAME=fourscribe-container
HOST_UNIT=fourscribe.service
PORT=8093
GRACE=20
SECRET_FILE=/opt/40zen/secrets/4scribe.env
HF_CACHE="$ROOT/model-cache/hf"

fail() { echo "FAIL: $*" >&2; exit 1; }
note() { echo "$*"; }

[ "$(id -u)" -eq 0 ] || fail "run as root/owner (systemd required)"
for cmd in docker curl ss systemctl; do
  command -v "$cmd" >/dev/null 2>&1 || fail "$cmd not found"
done
docker info -f '{{.ServerVersion}}' >/dev/null 2>&1 || fail "docker daemon not accessible"
[ -f "$SECRET_FILE" ] || fail "root-owned secret env file missing (not read/printed)"
[ -d "$HF_CACHE" ] || fail "model cache missing: $HF_CACHE"

OUT=$(ss -H -tn state established 'sport = :8093' 2>/dev/null) || \
  fail "cannot determine 8093 session state (ss failed); idle-only rollback refused"
[ -z "$OUT" ] || fail "active 8093 session(s) present; idle-only rollback refused"

IDS=$(docker ps -aq --filter "name=^/${CNAME}$" 2>/dev/null || true)
if [ -f "$COMPOSE" ]; then
  IDS="$IDS $(docker compose -f "$COMPOSE" -p "$PROJECT" ps -q 2>/dev/null || true)"
fi

STOPPED=0
for id in $IDS; do
  [ -n "$id" ] || continue
  SVC=$(docker inspect -f '{{index .Config.Labels "com.40zen.service"}}' "$id" 2>/dev/null || true)
  if [ "$SVC" = "4scribe" ]; then
    if [ "$(docker inspect -f '{{.State.Running}}' "$id" 2>/dev/null || true)" = "true" ]; then
      docker stop -t "$GRACE" "$id" >/dev/null 2>&1 || fail "failed to stop owned container $id"
      STOPPED=1
      note "stopped_owned_container=$id"
    fi
  else
    fail "refusing to touch foreign container $id (missing 4scribe ownership label)"
  fi
done
[ "$STOPPED" = 1 ] || note "owned_container=already_stopped_or_absent"

# The cutover baseline requires the host unit to have been enabled. Re-enable
# and start it; this is idempotent if it is already active.
systemctl enable "$HOST_UNIT" >/dev/null 2>&1 || fail "cannot enable $HOST_UNIT"
systemctl start "$HOST_UNIT" >/dev/null 2>&1 || fail "cannot start $HOST_UNIT"

HEALTHY=0
for _ in $(seq 1 60); do
  if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
    HEALTHY=1
    break
  fi
  sleep 1
done
[ "$HEALTHY" = 1 ] || fail "host unit started but /healthz is unavailable; inspect: systemctl status $HOST_UNIT"

note "rollback_result=host_unit_active host_healthz=ok"
note "container_state=stopped_preserved image_tag=preserved host_venv_models_token=preserved"
note "tailscale_route=unchanged"
