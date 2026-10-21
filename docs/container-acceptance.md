# Disposable container acceptance (mobile batch)

Scope: build the **current source** into a fresh image and prove the image-level
runtime on a separate loopback port. This is not a deployment and does not touch
`fourscribe-container`, the accepted image, Compose, routes or secrets.

Current status:

- **Local-dev integration: passed** (real M4A → ffmpeg → real Ollama
  `frozenlab/qwen3-asr:1.7b`, Silero on, aligner disabled; see the 2026-10-21
  section of the local-only resource registry).
- **Container acceptance: passed on source `a34f8ef`: 137 image tests, health
  and batch auth isolation; disposable container removed.**
- Owner subsequently promoted the same accepted image as
  `4scribe:accepted-2026-10-21`; batch isolation and Loqui WS handshake passed.
  Real Qwen audio integration was a separate dev-host test, not image smoke.
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
   suite and is not weakened or bundled into the STT image. On the current HEAD
   the image scope is **137 passed**, including the real-ffmpeg WAV/M4A batch
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
