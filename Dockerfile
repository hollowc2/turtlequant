FROM python:3.13-slim

# Install uv from the official image, pinned by version and digest. Keep it in
# step with the uv that writes uv.lock (lockfile revision 3 needs a recent uv).
COPY --from=ghcr.io/astral-sh/uv:0.11.16@sha256:440fd6477af86a2f1b38080c539f1672cd22acb1b1a47e321dba5158ab08864d /uv /uvx /usr/local/bin/

ENV UV_PROJECT_ENVIRONMENT=/app/.venv
# If anyone does `uv run` in the container, never re-resolve or add dev deps.
ENV UV_FROZEN=1
ENV UV_NO_DEV=1
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# --- Phase 1: install deps (cached unless lock/pyproject changes) ---
COPY pyproject.toml uv.lock .python-version ./

# Stub src dir so uv can resolve the package before source is copied
RUN mkdir -p src/turtlequant

# Install all third-party deps from lockfile (no dev deps)
RUN uv sync --frozen --no-install-project --no-dev

# --- Phase 2: copy source (invalidated on code changes, deps stay cached) ---
COPY src/     src/
COPY scripts/ scripts/

# Re-sync to install the turtlequant package now that source exists (fast: deps cached)
RUN uv sync --frozen --no-dev

# State dir for persistent files (bind-mounted at runtime via docker-compose)
RUN mkdir -p /app/state

# Run the synced venv's python directly: `uv run` would re-sync the project
# (dev group included by default) at every container start.
CMD ["python", "scripts/turtlequant_bot.py", "--shadow", "--asset", "btc,eth"]
