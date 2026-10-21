# Local development environment

Canonical checkout: `/opt/40zen/repos/4scribe` (the former
`/opt/40zen/services/4scribe` tree is no longer the source of truth; see
`deployment/compose.yaml` there for the immutable production runtime).

Production is **not** served from this checkout. `fourscribe-container` runs the
accepted image at `127.0.0.1:8093` with the read-only model cache under
`/opt/40zen/services/4scribe/model-cache/hf`. Do not restart, redeploy or edit
production from a development session.

## Python environment

`.venv` is a local, ignored, persistent development environment:

- system Python `3.14.4`, created with `--without-pip`
- pip bootstrapped from the official PyPA script
  `https://bootstrap.pypa.io/get-pip.py` (sha256
  `f4448a01e3ab2914c65f4ca59d4cacba6b2dbde19a483fbac46587f8bbaff424`,
  installs pip `26.2.2`)
- runtime/test pins installed from `requirements-container.txt` with
  `--extra-index-url https://download.pytorch.org/whl/cpu` (CPU torch only:
  `torch==2.14.1+cpu`; no CUDA packages)
- `qwen-asr==0.0.6` installed separately with `--no-deps`, exactly like the
  Dockerfile

Reproduce/update:

```sh
cd /opt/40zen/repos/4scribe
.venv/bin/python -m pip install \
  --extra-index-url https://download.pytorch.org/whl/cpu \
  -r requirements-container.txt
.venv/bin/python -m pip install --no-deps qwen-asr==0.0.6
```

There is no global pip/apt installation and no Docker group requirement.

## ffmpeg (development-only)

The host has no system ffmpeg. A static build lives in ignored `.dev-tools`:

- source: BtbN/FFmpeg-Builds release asset `latest/ffmpeg-n9.0-latest-linux64-gpl-9.0.tar.xz`
  (`https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/`)
- checksum file: `checksums.sha256` from the same release
- archive sha256: `293f28e27d79dfe5f83f988707a8c3c09df3d6e2b661ad7ad17ef8aeab3f0611`
  (verified with `sha256sum -c` before extraction)
- installed binary: `.dev-tools/ffmpeg-n9.0/bin/ffmpeg`
  (`ffmpeg version n9.0.2-29-g6dfaba896f-20261020`, GPL static build)

The Dockerfile installs distro ffmpeg for deployment. `.dev-tools` is only for
local tests/server runs and must not enter a build context or Git.

## Development `.env`

`.env` is ignored by Git and is **development-only**: fake realtime/mobile
tokens, `FSCRIBE_SILERO_ENABLED=0`, `FSCRIBE_PORT=18094`. `Config.from_env()`
does not load dotenv files, so source it in the shell for server runs:

```sh
cd /opt/40zen/repos/4scribe
set -a; . ./.env; set +a
PATH="$PWD/.dev-tools/ffmpeg-n9.0/bin:$PATH" PYTHONPATH=src \
  .venv/bin/python -m uvicorn fourscribe.app:create_app \
  --factory --host 127.0.0.1 --port 18094
```

Tests construct `Config` directly and do not need `.env`. Never read or copy
the production env file `/opt/40zen/secrets/4scribe.env` into development.

## Tests

Hermetic batch + regression suite (no Ollama server, no model downloads):

```sh
cd /opt/40zen/repos/4scribe
PATH="$PWD/.dev-tools/ffmpeg-n9.0/bin:$PATH" \
  .venv/bin/python -m pytest tests -q
```

Batch contract only:

```sh
PATH="$PWD/.dev-tools/ffmpeg-n9.0/bin:$PATH" \
  .venv/bin/python -m pytest tests/test_mobile_batch.py -q
```

The `real_ffmpeg*` tests run automatically when `ffmpeg` is on `PATH`; they
generate synthetic WAV/M4A audio and never download recordings. The full local
suite also includes `test_aligner_cache.py`, which only resolves/stat()s the
pre-provisioned read-only snapshot path under the production model cache; it
does not load weights.

Temporary bootstrap downloads (get-pip, ffmpeg archive, pip log) live in
`/tmp/opencode/4scribe-dev/`; they are not part of the repository or image.
