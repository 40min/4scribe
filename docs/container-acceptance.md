# Disposable container acceptance (mobile batch)

Scope: build the **current source** into a fresh image and prove the image-level
runtime on a separate loopback port. This is not a deployment and does not touch
`fourscribe-container`, the accepted image, Compose, routes or secrets.

Current status (enhancement candidate):

- **Container acceptance: passed on source `7717021`: 195 image tests**, real
  ffmpeg, health + batch auth isolation on disposable `127.0.0.1:18095`;
  container removed, image retained for review.
- Candidate tag `4scribe:dev-acceptance-7717021`; BuildKit config/image ID
  `sha256:006589f5e1a3c62363a8bd34e55d8f2be6e7ba90d1bd065a016ff4bfbdf7eac4`
  (manifest-list digest `sha256:2f8903baf787e78e24778a01e16943bf10286b71c1cb3f0e75e07ea8f1e306c3`).
  Acceptance log: `/tmp/4scribe-container-acceptance-7717021.log`.
- That acceptance container used fake WS/mobile tokens only and did **not**
  configure an enhancement token, so its health correctly reported enhancement
  disabled (`unset`). The image's enhancement tests ran in the hermetic suite.
- `deployment/enhancement-candidate-smoke.sh` is prepared to prove the
  candidate's enhancement health/auth isolation on a disposable listener
  (`18096`) with **fake** WS/mobile/enhancement tokens and no model call.
- `deployment/promote-enhancement.py` is prepared for a guarded root-owner
  promotion; it is **not executed** by this preparation.
- Real Qwen audio integration was a separate dev-host test, not image smoke.
  Mock/image auth tests do not prove model quality, and no production secret
  was used by candidate acceptance.
- `4scribe:dev-mobile` predates the final source/tests. Do **not** use it as
  evidence; the script builds a new `4scribe:dev-acceptance-<sha>` tag.

## Run

Requirements: Docker access (primary/owner), current checkout at
`/opt/40zen/repos/4scribe`, port **18095** free, and enough space for one image.
The Dockerfile already installs `ffmpeg`; the process runs as UID 10001 with a
read-only root and a writable `/tmp` tmpfs.

```sh
cd /opt/40zen/repos/4scribe
./deployment/container-acceptance.sh
```

The script:

1. builds `IMAGE=${IMAGE:-4scribe:dev-acceptance-$(git rev-parse --short HEAD)}`
   from the working tree;
2. checks `ffmpeg -version` and imports inside the image;
3. runs `pytest` with `--network none`, excluding the host-dependent suites:
   `tests/test_aligner_cache.py` (host read-only weight snapshot),
   `tests/test_diag_units.py` (local-only harness, not in the image) and
   `tests/test_trace_replay.py` (imports
   `deployment/replay/streaming_replay.py`, which is not copied into the image,
   and needs the external Loqui sidecar). Replay remains in the **host/dev**
   suite and is not weakened or bundled into the STT image. On source `7717021`
   the image scope is **195 passed**, including the real-ffmpeg WAV/M4A batch
   tests; requires `python-multipart`, `torch` and ffmpeg in the image;
4. starts a disposable `--read-only` container on `127.0.0.1:18095` with fake
   tokens (`acceptance-*-0000000000`), `FSCRIBE_ALIGNER_ENABLED=0` and
   `FSCRIBE_SILERO_ENABLED=0`, waits for `/healthz`, and asserts:
   unauthenticated **401**, realtime token **401**, mobile token accepted
   (body-less request **400/413**, i.e. auth passed without model work);
5. removes the disposable container on exit and leaves the image for review:
   `docker image rm 4scribe:dev-acceptance-<sha>` after inspection.

No model weights, production env file, production token or cloud service is
used by the script. Health does not load models.

Host/dev replay suite remains intact and is **not** part of image acceptance;
run it separately with its sidecar available:

```sh
PATH="$PWD/.dev-tools/ffmpeg-n9.0/bin:$PATH" \
  .venv/bin/python -m pytest tests/test_trace_replay.py -q
```

## Enhancement candidate smoke (prepared, fake tokens only)

This uses the already-built candidate image, does not build, does not touch the
production container, and calls no model:

```sh
cd /opt/40zen/repos/4scribe
./deployment/enhancement-candidate-smoke.sh
```

Defaults: candidate `4scribe:dev-acceptance-7717021`, disposable listener
`127.0.0.1:18096`, fake WS/mobile/enhancement tokens, `--read-only` container,
aligner/Silero disabled. It asserts pinned candidate image ID, enhancement
health enabled/unlatched, mobile configured, enhancement `401` without a token
and with WS/mobile tokens, enhancement token + invalid payload `422`, and the
existing mobile token still passing auth (body-less `400/413`). The enhancement
checks run on both the public prefixed route and the ingress-stripped
`/v1/text/enhance` alias. It removes the container and leaves the image
unchanged.

## Guarded production promotion (prepared, NOT executed)

Owner/root command after candidate review:

```sh
sudo python3 /opt/40zen/repos/4scribe/deployment/promote-enhancement.py
```

Pinned inputs: tested source `7717021`, candidate tag/ID above, previous
production tag `4scribe:accepted-2026-10-21` / image
`sha256:136387338841d76d58b155ae137a9c66f05a5820d8f882e2274bec20b3d07e84`,
new unique tag `4scribe:accepted-enhancement-2026-10-21`.

The script refuses unless source/inputs/container/compose match those pins and
there are no established connections on `127.0.0.1:8093`. It then, entirely
inside root memory/file access:

- preserves existing `FSCRIBE_TOKEN`/`FSCRIBE_MOBILE_TOKEN` and only
  appends/generates `FSCRIBE_ENHANCEMENT_TOKEN` if absent (validated distinct,
  non-placeholder, ≥16 chars); never prints or passes a secret on argv;
- updates only the runtime compose image tag, accepted-image label and
  `FSCRIBE_KEEP_ALIVE_S="-1"`, preserving host networking, non-root/read-only
  runtime, cache mount, tmpfs, stop/health settings;
- recreates the compose service and requires health to show aligner + Silero +
  mobile + enhancement configured, then checks enhancement auth isolation,
  existing mobile auth and the WS handshake;
- writes a non-secret receipt
  `/opt/40zen/services/4scribe/deployment/promotion-receipt-enhancement-2026-10-21.json`;
- on post-restart failure restores the previous runtime compose and recreates
  the previous accepted image. The previous tag/image is retained; a newly
  provisioned enhancement token remains in the root-0600 env file (no secret
  backup). New tag is never confused with the live accepted tag.

This preparation does not restart, deploy, read production secrets, change
Ollama/systemd/model lists, or switch Loqui. Legacy `parakeet-test` remains the
active client enhancement dependency until the separate Loqui endpoint/key
migration is accepted.

## Route-fix follow-up (prepared after promotion)

Production is already on `4scribe:accepted-enhancement-2026-10-21`. The
original `deployment/promote-enhancement.py` is a historical one-shot gate and
**must not be rerun**: its predecessor/source pins intentionally describe the
pre-enhancement state. Use this bounded delta for the route-fix candidate:

1. Build and accept the fixed source with `deployment/container-acceptance.sh`
   (new tag `4scribe:dev-acceptance-<fixsha>`, log under `/tmp`).
2. Prove the candidate with fake tokens on both route spellings:
   ```sh
   IMAGE=4scribe:dev-acceptance-<fixsha> \
   EXPECTED_IDS="<image ID from the build log>" \
     ./deployment/enhancement-candidate-smoke.sh
   ```
3. Primary/root delta after review, with no secret change/rotation:
   - require the live container to still be
     `4scribe:accepted-enhancement-2026-10-21` and no established connection
     on loopback:8093;
   - tag the reviewed candidate as the new unique local tag
     `4scribe:accepted-enhancement-routefix-2026-10-21`;
   - change only the runtime compose service image tag and
     `com.40zen.accepted-image` label (keep `FSCRIBE_KEEP_ALIVE_S: "-1"` and
     all cache/non-root/read-only/network settings);
   - recreate with
     `docker compose -f /opt/40zen/services/4scribe/deployment/compose.yaml -p fourscribe up -d --no-build --force-recreate`;
   - require health enhancement enabled/unlatched, `401` without a token on
     **both** `/4scribe/v1/text/enhance` and `/v1/text/enhance`, then repeat the
     Loqui settings save/chocolate test;
   - on any failure restore the previous runtime compose and recreate
     `4scribe:accepted-enhancement-2026-10-21`. Retain the previous tag/image.
4. Do not switch Loqui, change ingress/Tailscale, or alter the root-0600 token
   file in this delta. Commit/publish the canonical compose change separately.

## Optional real batch smoke (not part of the script)

If the primary also wants a containerized real-ASR request, use host networking,
the existing Ollama and a **fake** token, and keep the separate port:

```sh
docker run --rm --network host --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=512m \
  -e FSCRIBE_TOKEN=acceptance-realtime-0000000000 \
  -e FSCRIBE_MOBILE_TOKEN=acceptance-mobile-0000000000 \
  -e FSCRIBE_ALIGNER_ENABLED=0 \
  -e FSCRIBE_SILERO_ENABLED=1 \
  4scribe:dev-acceptance-<sha> --port 18095
```

Then POST one generated/public M4A to
`http://127.0.0.1:18095/4scribe/v1/audio/transcriptions` with the mobile Bearer.
Only if aligner behavior must be checked, add the existing read-only cache mount
`-v /opt/40zen/services/4scribe/model-cache/hf:/models/hf:ro`; the batch path
does not use it.
