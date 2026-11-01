FROM python:3.15-slim@sha256:94d3426f1def7db4a106a232ac121bfd975393e5dfd40a619b955fae45fe7913

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    chromium-driver \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock ./
# Exactly the lockfile, into /app/.venv on the image's own interpreter (never
# a downloaded one), runtime dependencies only. The venv's bin goes first on
# PATH so `python` below is the one that has them. uv is mounted for this step
# only; nothing runs it after the build, and copied in it was 52 MB of every
# image.
ENV UV_PYTHON_PREFERENCE=only-system \
    UV_LINK_MODE=copy
RUN --mount=from=ghcr.io/astral-sh/uv:0.9.27,source=/uv,target=/usr/local/bin/uv \
    uv sync --frozen --no-dev --no-cache

COPY . .

# The commit this image was built from, so every event and traceback the
# service records names its release. The build workflow passes it; a local
# build without it reads "unknown" rather than lying.
ARG GIT_SHA=unknown
ENV PATH="/app/.venv/bin:/usr/lib/chromium:${PATH}" \
    PYTHONPATH="/app/src" \
    JOBTRACKER_REVISION=${GIT_SHA}

# Every fleet compose file names its own command (uvicorn for the api,
# `python -m api.worker` for workers), so this is only the bare `docker run`
# default. Most containers are workers.
CMD ["python", "-m", "api.worker"]
