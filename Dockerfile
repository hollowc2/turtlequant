FROM python:3.13-slim

# Install uv from official image (pinned version for reproducibility)
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /usr/local/bin/

ENV UV_PROJECT_ENVIRONMENT=/app/.venv
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

CMD ["uv", "run", "python", "scripts/turtlequant_bot.py", "--paper", "--asset", "btc,eth"]
