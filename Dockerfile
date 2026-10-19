# 4scribe app image: FastAPI + in-process Silero VAD + Qwen3 forced aligner.
# Ollama is external (host loopback) and is NOT in this image.
# Base pinned to python:3.14.4-slim (matches the accepted host interpreter;
# cp314 torch==2.14.1+cpu manylinux_2_28_x86_64 wheel verified present).
FROM python:3.14.4-slim@sha256:2ca02f32b4d9d893863367ce07ec1972819f476dd38d8612f2a9cb6a41cbb727

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src \
    HOME=/tmp

WORKDIR /app

# Direct + critical transitive pins grounded in the host inventory
# (docs/pip-freeze-2026-10-16-completion.txt). No full host freeze dump.
COPY requirements-container.txt ./
RUN pip install --no-cache-dir \
      --extra-index-url https://download.pytorch.org/whl/cpu \
      -r requirements-container.txt
# Mirror the accepted host venv: qwen-asr is installed without its optional
# gradio/Flask UI dependency tree; its real runtime deps are pinned above.
RUN pip install --no-cache-dir --no-deps qwen-asr==0.0.6

COPY src ./src
COPY tests ./tests

# Non-root numeric runtime user (no passwd entry needed; HOME=/tmp). The image
# is intended to run with --read-only and a writable /tmp tmpfs; no model
# weights or token are baked in.
USER 10001:10001

# Read-only aligner cache is mounted at /models/hf; offline env forbids any
# runtime weight download. Silero JIT travels with its pinned wheel.
ENV FSCRIBE_ALIGNER_HF_HOME=/models/hf \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1

EXPOSE 18093

# Trial default is the isolated port 18093. Cutover must explicitly override
# with `--port 8093` after the owner gate; FSCRIBE_PORT alone does not
# override the uvicorn CLI.
ENTRYPOINT ["python", "-m", "uvicorn", "fourscribe.app:create_app", \
            "--factory", "--app-dir", "src", "--host", "127.0.0.1"]
CMD ["--port", "18093"]
