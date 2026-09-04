FROM python:3.13-slim@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    DISPLAY=:99

RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    chromium-driver \
    xvfb \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.9.27 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
# Exactly the lockfile, into /app/.venv on the image's own interpreter (never
# a downloaded one), runtime dependencies only. The venv's bin goes first on
# PATH so `python` below is the one that has them.
ENV UV_PYTHON_PREFERENCE=only-system \
    UV_LINK_MODE=copy
RUN uv sync --frozen --no-dev --no-cache

COPY . .

# The commit this image was built from, so every event and traceback the
# service records names its release. The build workflow passes it; a local
# build without it reads "unknown" rather than lying.
ARG GIT_SHA=unknown
ENV PATH="/app/.venv/bin:/usr/lib/chromium:${PATH}" \
    PYTHONPATH="/app/src" \
    JOBTRACKER_REVISION=${GIT_SHA}

CMD ["python", "-m", "trackers.run_tracker", "ft"]
