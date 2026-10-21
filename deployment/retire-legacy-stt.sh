#!/usr/bin/env bash
# Bounded retirement of the legacy Parakeet/Nemotron STT stack.
#
# PREPARED BUT NOT EXECUTED. Default mode only prints the plan. The destructive
# path requires root, explicit client-acceptance acknowledgement, and a typed
# confirmation environment variable. There is no backup/rollback copy.
#
# This script deliberately does not use or modify:
#   - deployment/retire-host-runtime.sh (pins an obsolete 4scribe image)
#   - services/parakeet-test/Makefile target `clean` (globally disables Serve)
#   - qwen3:0.6b, frozenlab/qwen3-asr:1.7b, embeddings, aligner weights
#   - the 4scribe container, image, model cache, ingress or credential
#   - Tailscale root `/` Gateway or `/4scribe`
#
# Review docs/legacy-retirement-plan.md before execution.

set -euo pipefail

SCRIPT_DIR=$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
readonly SCRIPT_DIR
readonly SERVE_GUARD="$SCRIPT_DIR/legacy_serve_guard.py"

readonly ACCEPTED_IMAGE='sha256:f313ab3a8c98696c611fc8ee88e0a9f8e858aad3f1d2442363fd27fb50daa474'
readonly FSCRIBE_CONTAINER='fourscribe-container'
readonly FSCRIBE_HEALTH='http://127.0.0.1:8093/healthz'
readonly FSCRIBE_COMPOSE='/opt/40zen/services/4scribe/deployment/compose.yaml'

readonly LEGACY_CONTAINERS=(parakeet-test parakeet-stt)
readonly LEGACY_NETWORK='parakeet_default'
readonly LEGACY_DIRS=(
  /opt/40zen/services/parakeet
  /opt/40zen/services/parakeet-test
)
readonly LEGACY_TMP_FILE='/tmp/opencode/parakeet-test/ru_test.wav'
readonly LEGACY_TMP_DIR='/tmp/opencode/parakeet-test'
readonly LEGACY_SECRETS=(
  /opt/40zen/secrets/stt.env
  /opt/40zen/secrets/parakeet-mobile.env
)

# Exact tags only; no repository wildcards. Verified by primary's bounded
# `docker image ls` inventory. Preflight refuses if any listed tag is absent, so
# a stale entry cannot silently delete something else.
readonly LEGACY_IMAGE_TAGS=(
  'parakeet-stt:0.2.0-a12b237'
  'parakeet-stt:latest'
  'parakeet-stt:0.2.0-b809bbb'
  'nemo-speech-runtime-cpu:0.2.0-a12b237'
  'nemo-speech-runtime-cpu:latest'
  'nemo-speech-runtime-cpu:0.2.0-b809bbb'
  'parakeet-test:latest'
  'parakeet-test:rollback-2026-10-14'
  'parakeet-test:pre-mobile-20261007'
)

readonly LEGACY_SERVE_PATHS=(
  '/parakeet-test'
  '/v1/audio/transcriptions/realtime'
)

MODE='plan'
CLIENTS_ACCEPTED=0
TMPDIR_RUN=''

usage() {
  cat <<'EOF'
Usage: retire-legacy-stt.sh [--plan|--check|--execute] [--clients-accepted]

  --plan              Print the exact retirement plan (default; no changes).
  --check             Local-only preflight: paths, symlinks, mounts, compose
                      guards. Does not contact Docker, Tailscale or the network.
  --execute           Destructive: require root, --clients-accepted and
                      RETIRE_LEGACY_STT=YES. Removes only the allowlisted legacy
                      routes, containers, network, images, directories, temp
                      fixture and two secret files. No backup is created.

Example after review:
  sudo env RETIRE_LEGACY_STT=YES \
    /opt/40zen/repos/4scribe/deployment/retire-legacy-stt.sh \
    --execute --clients-accepted
EOF
}

die() {
  echo "REFUSED: $*" >&2
  exit 1
}

cleanup() {
  if [[ -n "${TMPDIR_RUN:-}" && -d "$TMPDIR_RUN" ]]; then
    rm -rf --one-file-system -- "$TMPDIR_RUN"
  fi
}
trap cleanup EXIT

for arg in "$@"; do
  case "$arg" in
    --plan) MODE='plan' ;;
    --check) MODE='check' ;;
    --execute) MODE='execute' ;;
    --clients-accepted) CLIENTS_ACCEPTED=1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown argument: $arg" ;;
  esac
done

assert_no_symlink_ancestor() {
  local path=$1
  python3 - "$path" <<'PY'
import pathlib
import sys

p = pathlib.Path(sys.argv[1])
for ancestor in (p, *p.parents):
    if ancestor.is_symlink():
        raise SystemExit(f"symlink in path: {ancestor}")
PY
}

assert_canonical_dir() {
  local path=$1
  [[ -d "$path" ]] || die "missing directory: $path"
  [[ "$(realpath -e -- "$path")" == "$path" ]] || die "not canonical: $path"
  assert_no_symlink_ancestor "$path"
}

assert_no_mounts_beneath() {
  local path=$1
  local mounts_json
  mounts_json=$(findmnt --json --output TARGET) ||
    die 'cannot read mount table'
  python3 - "$path" "$mounts_json" <<'PY'
import json
import sys

target = sys.argv[1]


def targets(nodes):
    for node in nodes:
        yield node["target"]
        yield from targets(node.get("children", []))


doc = json.loads(sys.argv[2])
for candidate in targets(doc["filesystems"]):
    if candidate == target or candidate.startswith(target + "/"):
        raise SystemExit(f"mountpoint beneath target: {candidate}")
PY
}

require_line() {
  local file=$1 expected=$2
  grep -Fqx -- "$expected" "$file" >/dev/null ||
    die "missing expected line in $file: $expected"
}

local_checks() {
  local d
  for d in "${LEGACY_DIRS[@]}"; do
    assert_canonical_dir "$d"
    assert_no_mounts_beneath "$d"
  done

  require_line '/opt/40zen/services/parakeet/docker-compose.yml' '    image: parakeet-stt:latest'
  require_line '/opt/40zen/services/parakeet/docker-compose.yml' '    container_name: parakeet-stt'
  require_line '/opt/40zen/services/parakeet/docker-compose.yml' '      - "100.72.25.9:8090:8080"'
  require_line '/opt/40zen/services/parakeet/docker-compose.yml' '      - /opt/40zen/secrets/stt.env'

  require_line '/opt/40zen/services/parakeet-test/docker-compose.yml' '    image: parakeet-test:latest'
  require_line '/opt/40zen/services/parakeet-test/docker-compose.yml' '    container_name: parakeet-test'
  require_line '/opt/40zen/services/parakeet-test/docker-compose.yml' '    network_mode: host'
  require_line '/opt/40zen/services/parakeet-test/docker-compose.yml' '      - /opt/40zen/secrets/parakeet-mobile.env'

  [[ -f /opt/40zen/services/parakeet/models/nemotron-3.5-asr-streaming-0.6b.q8_0.gguf ]] ||
    die 'missing legacy Nemotron GGUF'
  [[ -f /opt/40zen/services/parakeet/models/parakeet-tdt-0.6b-v3.q8_0.gguf ]] ||
    die 'missing legacy TDT rollback GGUF'

  [[ -f "$LEGACY_TMP_FILE" ]] || die "missing expected temp fixture: $LEGACY_TMP_FILE"
  [[ -d "$LEGACY_TMP_DIR" ]] || die "missing expected temp directory: $LEGACY_TMP_DIR"
  assert_no_symlink_ancestor "$LEGACY_TMP_DIR"

  [[ -f "$FSCRIBE_COMPOSE" ]] || die "missing 4scribe runtime compose: $FSCRIBE_COMPOSE"
  grep -Fq 'image: 4scribe:accepted-enhancement-routefix-2026-10-21' "$FSCRIBE_COMPOSE" ||
    die '4scribe compose does not name the accepted routefix tag'
  grep -Fq "$ACCEPTED_IMAGE" "$FSCRIBE_COMPOSE" ||
    die '4scribe compose does not pin the accepted image ID label'

  # Secret paths are checked only in execute mode as root. Preparation and
  # --check never open or stat the secret directory.
}

print_plan() {
  cat <<EOF
Legacy STT retirement plan (no changes in this mode)
====================================================
Preserve: $FSCRIBE_CONTAINER ($ACCEPTED_IMAGE), 4scribe compose/cache/secret,
          Tailscale '/', '/4scribe', shared Ollama models/aligner.

Remove Tailscale Serve paths (443), preserving all other handlers:
  ${LEGACY_SERVE_PATHS[0]} -> http://127.0.0.1:8091
  ${LEGACY_SERVE_PATHS[1]} -> http://100.72.25.9:8090/v1/audio/transcriptions/realtime

Stop and remove containers (wrapper first):
  ${LEGACY_CONTAINERS[0]}
  ${LEGACY_CONTAINERS[1]}

Remove exact compose network after it has no endpoints:
  $LEGACY_NETWORK

Remove exact image tags (no wildcards; confirm the final list from primary):
$(printf '  %s\n' "${LEGACY_IMAGE_TAGS[@]}")

Remove exact directories and the known playground fixture:
$(printf '  %s\n' "${LEGACY_DIRS[@]}")
  $LEGACY_TMP_FILE
  $LEGACY_TMP_DIR (only if empty)

Remove dedicated legacy credential paths (paths only; values never read):
$(printf '  %s\n' "${LEGACY_SECRETS[@]}")

Post-check: 4scribe image/health/restart count unchanged; legacy routes,
containers, network, images, dirs and secret paths absent; no listeners on
8090/8091; 127.0.0.1:8093 still listening.
EOF
}

docker_preflight() {
  command -v docker >/dev/null 2>&1 || die 'docker CLI not found'
  docker info >/dev/null 2>&1 || die 'Docker daemon not accessible'

  local image status health restarts
  read -r image status health restarts < <(
    docker inspect --format '{{.Image}} {{.State.Status}} {{.State.Health.Status}} {{.RestartCount}}' "$FSCRIBE_CONTAINER"
  ) || die "cannot inspect $FSCRIBE_CONTAINER"
  [[ "$image" == "$ACCEPTED_IMAGE" ]] || die "4scribe image changed: $image"
  [[ "$status" == 'running' ]] || die "4scribe is not running: $status"
  [[ "$health" == 'healthy' ]] || die "4scribe is not healthy: $health"
  [[ "$restarts" =~ ^[0-9]+$ ]] || die "unexpected restart count: $restarts"
  FSCRIBE_RESTARTS_PRE=$restarts

  local mounts
  mounts=$(docker inspect --format '{{json .Mounts}}' "$FSCRIBE_CONTAINER") ||
    die "cannot inspect $FSCRIBE_CONTAINER mounts"
  python3 - "$mounts" <<'PY'
import json
import sys

mounts = json.loads(sys.argv[1])
if len(mounts) != 1:
    raise SystemExit(f"expected exactly one 4scribe mount, got {len(mounts)}")
m = mounts[0]
expected = {
    "Type": "bind",
    "Source": "/opt/40zen/services/4scribe/model-cache/hf",
    "Destination": "/models/hf",
    "RW": False,
}
for key, value in expected.items():
    if m.get(key) != value:
        raise SystemExit(f"unexpected 4scribe mount {key}: {m.get(key)!r}")
PY

  python3 - "$FSCRIBE_HEALTH" <<'PY'
import json
import sys
import urllib.request

with urllib.request.urlopen(sys.argv[1], timeout=5) as response:
    if response.status != 200:
        raise SystemExit(f"health HTTP {response.status}")
    body = json.load(response)
if body.get("ok") is not True:
    raise SystemExit("health did not report ok=true")
if body.get("aligner") != "qwen3-forced-aligner-0.6b":
    raise SystemExit(f"unexpected aligner: {body.get('aligner')!r}")
if body.get("word_timestamps") is not True:
    raise SystemExit("word_timestamps is not true")
PY
}

assert_container() {
  local name=$1 expected_image=$2 expected_project=$3 expected_service=$4 expected_network=$5
  local out actual_name actual_image project service netmode image_id
  out=$(docker inspect --format \
    '{{.Name}}|{{.Config.Image}}|{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}|{{.HostConfig.NetworkMode}}|{{.Image}}' \
    "$name") || die "cannot inspect legacy container: $name"
  IFS='|' read -r actual_name actual_image project service netmode image_id <<< "$out"
  [[ "$actual_name" == "/$name" ]] || die "unexpected container name: $actual_name"
  [[ "$actual_image" == "$expected_image" ]] || die "$name uses image $actual_image"
  [[ "$project" == "$expected_project" ]] || die "$name project label is $project"
  [[ "$service" == "$expected_service" ]] || die "$name service label is $service"

  local tag_id
  tag_id=$(docker image inspect --format '{{.Id}}' "$expected_image") ||
    die "cannot resolve image $expected_image"
  [[ "$image_id" == "$tag_id" ]] || die "$name image ID does not match $expected_image"

  local networks
  networks=$(docker inspect --format '{{json .NetworkSettings.Networks}}' "$name") ||
    die "cannot inspect $name networks"
  python3 - "$name" "$expected_network" "$networks" <<'PY'
import json
import sys

name, expected, raw = sys.argv[1], sys.argv[2], sys.argv[3]
keys = sorted(json.loads(raw).keys())
if expected == "host":
    if keys not in ([], ["host"]):
        raise SystemExit(f"{name} is not on host network: {keys}")
else:
    if keys != [expected]:
        raise SystemExit(f"{name} networks {keys} != [{expected}]")
PY

  local mounts
  mounts=$(docker inspect --format '{{json .Mounts}}' "$name") ||
    die "cannot inspect $name mounts"
  python3 - "$name" "$mounts" <<'PY'
import json
import sys

name, raw = sys.argv[1], sys.argv[2]
mounts = json.loads(raw)
if name == "parakeet-test":
    if mounts:
        raise SystemExit(f"parakeet-test unexpectedly has mounts: {mounts}")
elif name == "parakeet-stt":
    if len(mounts) != 1:
        raise SystemExit(f"parakeet-stt mount count is {len(mounts)}")
    m = mounts[0]
    expected = {
        "Type": "bind",
        "Source": "/opt/40zen/services/parakeet/models/nemotron-3.5-asr-streaming-0.6b.q8_0.gguf",
        "Destination": "/models/nemotron-3.5-asr-streaming-0.6b.q8_0.gguf",
        "RW": False,
    }
    for key, value in expected.items():
        if m.get(key) != value:
            raise SystemExit(f"unexpected parakeet-stt mount {key}: {m.get(key)!r}")
else:
    raise SystemExit(f"unexpected container: {name}")
PY
}

assert_port_binding() {
  local raw
  raw=$(docker inspect --format '{{json .HostConfig.PortBindings}}' parakeet-stt) ||
    die 'cannot inspect parakeet-stt port bindings'
  python3 - "$raw" <<'PY'
import json
import sys

bindings = json.loads(sys.argv[1])
expected = {"8080/tcp": [{"HostIp": "100.72.25.9", "HostPort": "8090"}]}
if bindings != expected:
    raise SystemExit(f"unexpected parakeet-stt port bindings: {bindings}")
PY
}

serve_preflight() {
  command -v tailscale >/dev/null 2>&1 || die 'tailscale CLI not found'
  [[ -f "$SERVE_GUARD" ]] || die "missing Serve guard: $SERVE_GUARD"
  tailscale serve status --json > "$TMPDIR_RUN/serve-pre.json" ||
    die 'cannot read Tailscale Serve status'
  python3 "$SERVE_GUARD" pre "$TMPDIR_RUN/serve-pre.json"
}

serve_postcheck() {
  tailscale serve status --json > "$TMPDIR_RUN/serve-post.json" ||
    die 'cannot read post-retirement Serve status'
  python3 "$SERVE_GUARD" post \
    "$TMPDIR_RUN/serve-pre.json" "$TMPDIR_RUN/serve-post.json"
}

execute_preflight() {
  [[ $EUID -eq 0 ]] || die '--execute requires root'
  local_checks
  docker_preflight
  serve_preflight

  local c
  for c in "${LEGACY_CONTAINERS[@]}"; do
    docker inspect "$c" >/dev/null 2>&1 || die "missing legacy container: $c"
  done
  assert_container 'parakeet-test' 'parakeet-test:latest' 'parakeet-test' 'parakeet-test' 'host'
  assert_container 'parakeet-stt' 'parakeet-stt:latest' 'parakeet' 'parakeet' 'parakeet_default'
  assert_port_binding

  local attached
  attached=$(docker network inspect --format '{{json .Containers}}' "$LEGACY_NETWORK") ||
    die "missing legacy network: $LEGACY_NETWORK"
  python3 - "$attached" <<'PY'
import json
import sys

containers = json.loads(sys.argv[1])
names = sorted(c.get("Name", "") for c in containers.values())
if names != ["parakeet-stt"]:
    raise SystemExit(f"legacy network has unexpected endpoints: {names}")
PY

  local tag used name
  for tag in "${LEGACY_IMAGE_TAGS[@]}"; do
    docker image inspect "$tag" >/dev/null 2>&1 || die "missing image tag: $tag"
    used=$(docker ps -a --filter "ancestor=$tag" --format '{{.Names}}' | sort -u)
    while IFS= read -r name; do
      [[ -z "$name" ]] && continue
      [[ "$name" == 'parakeet-stt' || "$name" == 'parakeet-test' ]] ||
        die "image $tag is used by unexpected container: $name"
    done <<< "$used"
  done

  local f
  for f in "${LEGACY_SECRETS[@]}"; do
    [[ -f "$f" && ! -L "$f" ]] || die "missing or symlinked legacy secret path: $f"
  done
}

do_execute() {
  echo '==> Removing two legacy Tailscale Serve paths only'
  local path
  for path in "${LEGACY_SERVE_PATHS[@]}"; do
    tailscale serve --yes --https=443 --set-path="$path" off
  done
  serve_postcheck

  echo '==> Stopping and removing legacy containers (wrapper first)'
  docker stop --time 20 "${LEGACY_CONTAINERS[0]}" "${LEGACY_CONTAINERS[1]}"
  docker rm "${LEGACY_CONTAINERS[0]}" "${LEGACY_CONTAINERS[1]}"

  echo '==> Removing empty legacy compose network'
  docker network rm "$LEGACY_NETWORK"

  echo '==> Removing exact legacy image tags'
  docker image rm "${LEGACY_IMAGE_TAGS[@]}"

  echo '==> Rechecking exact directories immediately before deletion'
  local d
  for d in "${LEGACY_DIRS[@]}"; do
    assert_canonical_dir "$d"
    assert_no_mounts_beneath "$d"
  done
  for d in "${LEGACY_DIRS[@]}"; do
    du -sB1 -- "$d"
    rm -rf --one-file-system -- "$d"
  done

  echo '==> Removing known legacy playground fixture'
  rm -f -- "$LEGACY_TMP_FILE"
  rmdir -- "$LEGACY_TMP_DIR" 2>/dev/null || true

  echo '==> Removing dedicated legacy credential paths (values never read)'
  rm -f -- "${LEGACY_SECRETS[@]}"
}

postcheck() {
  local image state health restarts c tag d f
  read -r image state health restarts < <(
    docker inspect --format '{{.Image}} {{.State.Status}} {{.State.Health.Status}} {{.RestartCount}}' "$FSCRIBE_CONTAINER"
  ) || die "cannot inspect $FSCRIBE_CONTAINER after retirement"
  [[ "$image" == "$ACCEPTED_IMAGE" ]] || die "4scribe image changed: $image"
  [[ "$state" == 'running' && "$health" == 'healthy' ]] ||
    die "4scribe not running/healthy after retirement: $state/$health"
  [[ "$restarts" == "$FSCRIBE_RESTARTS_PRE" ]] ||
    die "4scribe restarted during retirement: $FSCRIBE_RESTARTS_PRE -> $restarts"

  python3 - "$FSCRIBE_HEALTH" <<'PY'
import json
import sys
import urllib.request

with urllib.request.urlopen(sys.argv[1], timeout=5) as response:
    if response.status != 200:
        raise SystemExit(f"health HTTP {response.status}")
    body = json.load(response)
if body.get("ok") is not True:
    raise SystemExit("health did not report ok=true")
PY

  for c in "${LEGACY_CONTAINERS[@]}"; do
    if docker inspect "$c" >/dev/null 2>&1; then
      die "legacy container still present: $c"
    fi
  done
  if docker network inspect "$LEGACY_NETWORK" >/dev/null 2>&1; then
    die "legacy network still present: $LEGACY_NETWORK"
  fi
  for tag in "${LEGACY_IMAGE_TAGS[@]}"; do
    if docker image inspect "$tag" >/dev/null 2>&1; then
      die "legacy image tag still present: $tag"
    fi
  done
  for d in "${LEGACY_DIRS[@]}"; do
    [[ ! -e "$d" ]] || die "legacy directory still present: $d"
  done
  for f in "${LEGACY_SECRETS[@]}"; do
    [[ ! -e "$f" ]] || die "legacy secret path still present: $f"
  done
  [[ ! -e "$LEGACY_TMP_FILE" ]] || die "legacy temp fixture still present: $LEGACY_TMP_FILE"

  if ss -H -tln | grep -Eq ':(8090|8091)[[:space:]]'; then
    die 'legacy listener still present on 8090/8091'
  fi
  ss -H -tln | grep -q '127.0.0.1:8093' ||
    die '4scribe listener 127.0.0.1:8093 is missing'

  serve_postcheck
  echo 'retirement_complete: legacy STT absent; 4scribe healthy; no rollback copy created'
}

case "$MODE" in
  plan)
    print_plan
    ;;
  check)
    local_checks
    print_plan
    echo
    echo 'local_preflight=passed (Docker, Tailscale and secret checks are execute-only)'
    ;;
  execute)
    [[ $CLIENTS_ACCEPTED -eq 1 ]] ||
      die 'client acceptance not acknowledged; pass --clients-accepted after primary review'
    [[ "${RETIRE_LEGACY_STT:-}" == 'YES' ]] ||
      die 'set RETIRE_LEGACY_STT=YES to confirm irreversible retirement without backup'
    TMPDIR_RUN=$(mktemp -d /tmp/retire-legacy-stt.XXXXXX)
    execute_preflight
    do_execute
    postcheck
    ;;
esac
