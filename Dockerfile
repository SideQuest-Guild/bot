# syntax=docker/dockerfile:1

FROM python:3.15-slim AS runtime

# uv installs/syncs the project; copied from the official distroless uv image, not installed via curl|sh.
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# git is needed at build time only: sidequest-common/sidequest-api are fetched from GitHub over git.
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

# Dependency manifests + full source (uv_build needs src/ present to build the editable install, so a
# manifests-only pre-sync layer isn't viable here). sidequest-common and sidequest-api are fetched over git.
COPY pyproject.toml uv.lock .python-version ./
COPY src src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# The app only reads /app (bytecode is precompiled), so files stay root-owned. A `chown -R /app` here would copy
# the whole venv into a second large layer.
RUN groupadd --system sidequest && useradd --system --gid sidequest --home /app --shell /usr/sbin/nologin sidequest
USER sidequest

ENV PATH="/app/.venv/bin:${PATH}"

# The bot serves no HTTP port, so there's no HEALTHCHECK here.
CMD ["sidequest-bot"]
