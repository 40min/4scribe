#!/usr/bin/env python3
"""Guarded owner/root promotion of the tested 4scribe enhancement image.

Prepared, NOT executed by the remote agent. Run only after the independent
candidate acceptance and disposable fake-token smoke are reviewed.

What it does, in order:
  1. verifies the tested candidate tag resolves to one of the pinned immutable
     IDs from the acceptance build, source has not changed since that build,
     and the current production container/compose match the pinned predecessor;
  2. refuses if any established TCP connection exists on loopback:8093;
  3. as root, preserves FSCRIBE_TOKEN/FSCRIBE_MOBILE_TOKEN in the root-0600 env
     file and only appends/generates FSCRIBE_ENHANCEMENT_TOKEN when absent;
  4. tags the candidate under a NEW unique local tag (never overwrites the
     currently accepted tag), updates the runtime compose image/label and sets
     FSCRIBE_KEEP_ALIVE_S="-1", preserving all other runtime settings;
  5. recreates the compose service, then requires health to show aligner +
     Silero + mobile + enhancement configured, plus auth isolation and existing
     mobile/WS handshake checks;
  6. writes a non-secret receipt. On any post-restart failure it restores the
     previous runtime compose and recreates the previous accepted image. The
     previous image/tag is retained; a newly provisioned enhancement token is
     preserved (never written to a backup).

Never prints or passes secret values on the command line. No model/audio test,
no Ollama setting/model-list change, no registry push.
"""
import argparse
import base64
import json
import os
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path("/opt/40zen/repos/4scribe")
RUNTIME_COMPOSE = Path("/opt/40zen/services/4scribe/deployment/compose.yaml")
CANONICAL_COMPOSE = REPO / "deployment/compose.yaml"
ENV_FILE = Path("/opt/40zen/secrets/4scribe.env")
RECEIPT = Path("/opt/40zen/services/4scribe/deployment/"
               "promotion-receipt-enhancement-2026-10-21.json")

#: Candidate built and accepted from source 7717021 (see container log).
CANDIDATE_SOURCE_COMMIT = "7717021ee6ecd607fa5c95809b3abc843870a0ba"
CANDIDATE_TAG = "4scribe:dev-acceptance-7717021"
#: BuildKit config digest is the local image ID; the manifest-list digest is
#: accepted too for Docker-version differences.
CANDIDATE_IMAGE_IDS = {
    "sha256:006589f5e1a3c62363a8bd34e55d8f2be6e7ba90d1bd065a016ff4bfbdf7eac4",
    "sha256:2f8903baf787e78e24778a01e16943bf10286b71c1cb3f0e75e07ea8f1e306c3",
}
PREVIOUS_TAG = "4scribe:accepted-2026-10-21"
PREVIOUS_IMAGE_ID = ("sha256:136387338841d76d58b155ae137a9c66f05a5820d8"
                     "f882e2274bec20b3d07e84")
NEW_TAG = "4scribe:accepted-enhancement-2026-10-21"
SERVICE = "fourscribe"
CONTAINER = "fourscribe-container"
PORT = 8093
PLACEHOLDER_TOKENS = {"test-token", "changeme", "change-me"}
MIN_TOKEN_LEN = 16


class PromotionError(RuntimeError):
    pass


def fail(message):
    raise PromotionError(message)


def run(args, *, timeout=60, check=True):
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        hint = detail[-1][:200] if detail else "no output"
        fail(f"command failed ({Path(args[0]).name}): {hint}")
    return result


def docker_image_id(reference):
    result = run(["docker", "image", "inspect", "--format", "{{.Id}}", reference],
                 check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def container_image_id():
    result = run(["docker", "inspect", "--format", "{{.Image}}", CONTAINER],
                 check=False)
    if result.returncode != 0:
        fail(f"production container {CONTAINER} is not inspectable")
    return result.stdout.strip()


def check_source_and_compose():
    head = run(["git", "-C", str(REPO), "rev-parse", "HEAD"]).stdout.strip()
    in_history = run(["git", "-C", str(REPO), "merge-base", "--is-ancestor",
                      CANDIDATE_SOURCE_COMMIT, head], check=False)
    if in_history.returncode != 0:
        fail("tested candidate source commit is not in current history")
    no_source_drift = run(["git", "-C", str(REPO), "diff", "--quiet",
                           CANDIDATE_SOURCE_COMMIT, head, "--",
                           "src", "tests", "requirements-container.txt",
                           "Dockerfile"], check=False)
    if no_source_drift.returncode != 0:
        fail("source/tests/image inputs changed after the tested candidate build")

    changed = run(["git", "-C", str(REPO), "status", "--porcelain"]).stdout.splitlines()
    unexpected = [line for line in changed
                  if line[3:].strip() != "deployment/compose.yaml"]
    if unexpected:
        fail("unexpected working-tree changes: " +
             ", ".join(line[3:].strip() for line in unexpected))

    canonical = CANONICAL_COMPOSE.read_text(encoding="utf-8")
    if 'FSCRIBE_KEEP_ALIVE_S: "-1"' not in canonical:
        fail('canonical compose does not carry the desired FSCRIBE_KEEP_ALIVE_S: "-1"')


def check_predecessor():
    tag_id = docker_image_id(PREVIOUS_TAG)
    if tag_id != PREVIOUS_IMAGE_ID:
        fail("previous accepted tag does not resolve to the pinned previous image")
    if container_image_id() != tag_id:
        fail("production container does not match the pinned previous accepted image")

    text = RUNTIME_COMPOSE.read_text(encoding="utf-8")
    if f"image: {PREVIOUS_TAG}" not in text:
        fail("runtime compose does not use the pinned previous accepted tag")
    if f'com.40zen.accepted-image: "{PREVIOUS_IMAGE_ID}"' not in text:
        fail("runtime compose accepted-image label does not match the predecessor")
    for snippet in (
            'user: "10001:10001"', "read_only: true",
            "source: /opt/40zen/services/4scribe/model-cache/hf",
            "no-new-privileges:true", "network_mode: host", "pull_policy: never"):
        if snippet not in text:
            fail(f"runtime compose is missing required setting: {snippet}")
    if "FSCRIBE_ENHANCEMENT_TOKEN" in text:
        fail("enhancement token must come from the env file, not compose")
    return text


def check_candidate():
    candidate_id = docker_image_id(CANDIDATE_TAG)
    if candidate_id not in CANDIDATE_IMAGE_IDS:
        fail("candidate tag is missing or does not match the accepted immutable ID")
    existing_new = docker_image_id(NEW_TAG)
    if existing_new is not None and existing_new != candidate_id:
        fail("new promotion tag already exists with a different image")
    return candidate_id


def check_no_active_ws():
    result = run(["ss", "-Htn", "state", "established",
                  f"( sport = :{PORT} )"], check=False)
    if result.returncode != 0:
        fail("cannot verify idle state: ss failed (fail closed before restart)")
    active = [line for line in result.stdout.splitlines() if line.strip()]
    if active:
        fail(f"{len(active)} established connection(s) on loopback:{PORT}; "
             "close WS/HTTP clients and retry")


def read_env():
    info = ENV_FILE.stat()
    if info.st_uid != 0:
        fail("env file must be root-owned")
    if stat.S_IMODE(info.st_mode) != 0o600:
        fail("env file must be mode 0600")
    text = ENV_FILE.read_text(encoding="utf-8")
    values = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return text, values


def safe_token(value):
    return (len(value) >= MIN_TOKEN_LEN
            and value.lower() not in PLACEHOLDER_TOKENS)


def put_env_value(text, value):
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == "FSCRIBE_ENHANCEMENT_TOKEN":
            newline = "\r\n" if line.endswith("\r\n") else "\n"
            lines[index] = f"FSCRIBE_ENHANCEMENT_TOKEN={value}{newline}"
            return "".join(lines)
    if text and not text.endswith("\n"):
        text += "\n"
    return text + f"FSCRIBE_ENHANCEMENT_TOKEN={value}\n"


def atomic_write(path, text):
    info = path.stat()
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        os.fchmod(fd, stat.S_IMODE(info.st_mode))
        os.fchown(fd, info.st_uid, info.st_gid)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def provision_token(text, values):
    ws_token = values.get("FSCRIBE_TOKEN", "").strip()
    mobile_token = values.get("FSCRIBE_MOBILE_TOKEN", "").strip()
    if not ws_token or not mobile_token:
        fail("existing WS/mobile tokens must already be present; refusing to rotate")

    enhancement = values.get("FSCRIBE_ENHANCEMENT_TOKEN", "").strip()
    if enhancement:
        if not safe_token(enhancement):
            fail("existing enhancement token is too short or a placeholder")
        if enhancement == ws_token or enhancement == mobile_token:
            fail("existing enhancement token is not distinct from WS/mobile")
        return text, enhancement, "preserved"

    while True:
        enhancement = secrets.token_urlsafe(32)
        if (safe_token(enhancement)
                and enhancement != ws_token and enhancement != mobile_token):
            break
    updated = put_env_value(text, enhancement)
    atomic_write(ENV_FILE, updated)
    info = ENV_FILE.stat()
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
        fail("env file permissions changed unexpectedly")
    return updated, enhancement, "generated"


def rewrite_compose(text, candidate_id):
    lines = text.splitlines(keepends=True)
    seen_image = seen_label = seen_keep = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        indent = line[:len(line) - len(line.lstrip())]
        if stripped == f"image: {PREVIOUS_TAG}" and not seen_image:
            lines[index] = f"{indent}image: {NEW_TAG}\n"
            seen_image = True
        elif stripped.startswith("com.40zen.accepted-image:") and not seen_label:
            lines[index] = f'{indent}com.40zen.accepted-image: "{candidate_id}"\n'
            seen_label = True
        elif stripped.startswith("FSCRIBE_KEEP_ALIVE_S:"):
            lines[index] = f'{indent}FSCRIBE_KEEP_ALIVE_S: "-1"\n'
            seen_keep = True
    if not (seen_image and seen_label and seen_keep):
        fail("runtime compose did not contain the expected promotion anchors")
    return "".join(lines)


def compose_up():
    return run(["docker", "compose", "-f", str(RUNTIME_COMPOSE), "-p", SERVICE,
                "up", "-d", "--no-build", "--force-recreate"], timeout=180)


def get_json(path):
    request = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}",
                                     method="GET")
    with urllib.request.urlopen(request, timeout=4) as response:
        return response.status, json.loads(response.read())


def wait_health(timeout=120):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            status, payload = get_json("/healthz")
            if status == 200 and payload.get("ok") is True:
                return payload
            last = f"status={status}"
        except Exception as exc:  # noqa: BLE001 - bounded startup wait
            last = type(exc).__name__
        time.sleep(1)
    fail(f"promoted container did not become healthy ({last})")


def post(path, token=None, body=b"", content_type=None):
    headers = {}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    if content_type is not None:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}",
                                     data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def check_health(payload):
    if payload.get("aligner") != "qwen3-forced-aligner-0.6b":
        fail("health does not show the expected aligner")
    if payload.get("verifier") != "silero-vad-6.2.3":
        fail("health does not show Silero configured")
    if payload.get("mobile_batch") != {"enabled": True, "reason": "ok"}:
        fail("health does not show mobile batch configured")
    enhancement = payload.get("text_enhancement") or {}
    if (enhancement.get("enabled") is not True
            or enhancement.get("reason") != "ok"
            or enhancement.get("completion_unknown") is not False):
        fail("health does not show enhancement configured and unlatched")


def check_auth(ws_token, mobile_token, enhancement_token):
    enhance_path = "/4scribe/v1/text/enhance"
    for token in (None, ws_token, mobile_token):
        status, body = post(enhance_path, token, b'{"text": ""}',
                            "application/json")
        if status != 401 or json.loads(body) != {"error": {"code": "unauthorized"}}:
            fail("enhancement auth isolation failed")
    status, body = post(enhance_path, enhancement_token, b'{"text": ""}',
                        "application/json")
    if status != 422 or json.loads(body) != {"error": {"code": "invalid_input"}}:
        fail("enhancement token/invalid-payload check failed")

    status, _body = post("/4scribe/v1/audio/transcriptions", mobile_token)
    if status not in (400, 413):
        fail("existing mobile token no longer passes auth")

    key = base64.b64encode(os.urandom(16)).decode()
    request = (
        "GET /4scribe/v1/audio/transcriptions/realtime HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{PORT}\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
        f"Authorization: Bearer {ws_token}\r\n\r\n").encode()
    with socket.create_connection(("127.0.0.1", PORT), timeout=5) as conn:
        conn.sendall(request)
        status_line = conn.recv(1024).split(b"\r\n", 1)[0]
    if b" 101 " not in status_line:
        fail("existing realtime WS handshake no longer passes")


def write_receipt(status, candidate_id, token_action, checks, error=None):
    payload = {
        "status": status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source_commit": CANDIDATE_SOURCE_COMMIT,
        "candidate_tag": CANDIDATE_TAG,
        "candidate_image_id": candidate_id,
        "previous_tag": PREVIOUS_TAG,
        "previous_image_id": PREVIOUS_IMAGE_ID,
        "new_tag": NEW_TAG,
        "enhancement_token": token_action,
        "checks": checks,
        "previous_image_retained": True,
        "error": error,
    }
    RECEIPT.parent.mkdir(parents=True, exist_ok=True)
    RECEIPT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.chmod(RECEIPT, 0o644)


def rollback(previous_text, previous_image_id):
    atomic_write(RUNTIME_COMPOSE, previous_text)
    compose_up()
    payload = wait_health()
    if payload.get("ok") is not True:
        fail("rollback container did not report liveness")
    if container_image_id() != previous_image_id:
        fail("rollback recreated a container with an unexpected image")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    if os.geteuid() != 0:
        fail("must run as root (owner command: sudo python3 ...)")

    checks = {"preflight": False, "restarted": False, "health": False,
              "auth": False}
    candidate_id = None
    token_action = "not-provisioned"
    previous_text = None
    try:
        check_source_and_compose()
        previous_text = check_predecessor()
        candidate_id = check_candidate()
        check_no_active_ws()
        env_text, values = read_env()
        env_text, enhancement_token, token_action = provision_token(env_text, values)
        ws_token = values["FSCRIBE_TOKEN"].strip()
        mobile_token = values["FSCRIBE_MOBILE_TOKEN"].strip()
        checks["preflight"] = True

        if docker_image_id(NEW_TAG) is None:
            run(["docker", "tag", CANDIDATE_TAG, NEW_TAG])
        if docker_image_id(NEW_TAG) not in CANDIDATE_IMAGE_IDS:
            fail("new promotion tag does not resolve to the candidate image")

        rewritten = rewrite_compose(previous_text, candidate_id)
        atomic_write(RUNTIME_COMPOSE, rewritten)
        checks["restarted"] = True
        compose_up()

        payload = wait_health()
        check_health(payload)
        checks["health"] = True
        check_auth(ws_token, mobile_token, enhancement_token)
        checks["auth"] = True
        if container_image_id() not in CANDIDATE_IMAGE_IDS:
            fail("running container does not match the promoted candidate image")

        write_receipt("promoted", candidate_id, token_action, checks)
        print(f"PROMOTED {NEW_TAG}")
        print(f"receipt: {RECEIPT}")
        print(f"previous image retained: {PREVIOUS_TAG} ({PREVIOUS_IMAGE_ID})")
    except Exception as exc:  # noqa: BLE001 - must attempt bounded rollback
        error = str(exc) if isinstance(exc, PromotionError) else type(exc).__name__
        if checks["restarted"] and previous_text is not None:
            try:
                rollback(previous_text, PREVIOUS_IMAGE_ID)
                error += "; rollback restored previous accepted image"
            except Exception as rollback_exc:  # noqa: BLE001 - report manual action
                error += f"; ROLLBACK FAILED: {type(rollback_exc).__name__}"
                print("ROLLBACK FAILED: restore runtime compose and run "
                      "docker compose up -d --no-build --force-recreate manually",
                      file=sys.stderr)
        try:
            write_receipt("failed", candidate_id, token_action, checks, error)
        except Exception:  # noqa: BLE001 - preserve the primary error
            pass
        print(f"FAILED: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
