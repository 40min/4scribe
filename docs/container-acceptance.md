# Disposable container acceptance (mobile batch)

Scope: build the **current source** into a fresh image and prove the image-level
runtime on a separate loopback port. This is not a deployment and does not touch
`fourscribe-container`, the accepted image, Compose, routes or secrets.

Current status — accepted 2026-10-21:

- Source `e9ca825`: **197 image tests**, **114 focused tests** passed.
- Current image: `sha256:f313ab3a8c98696c611fc8ee88e0a9f8e858aad3f1d2442363fd27fb50daa474`,
  tag `4scribe:accepted-enhancement-routefix-2026-10-21`.
- Both enhancement route spellings support prefix-stripping ingress.
- Owner Loqui meeting `4dd5a489`, 15:46:31–15:47:21 UTC: two HTTP 200 unchanged
  outputs (110/105 chars), no errors/busy/timeouts/latch/restarts. Integration
  accepted, not quality improvement or a new full host-suite claim.
- Completed one-shot promotion helpers removed. Reusable candidate smoke and
  acceptance remain. Inline enhancement and legacy retirement remain undone.

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
   suite and is not weakened or bundled into the STT image. On source `e9ca825`
   the image scope is **197 passed**, including the real-ffmpeg WAV/M4A batch
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
