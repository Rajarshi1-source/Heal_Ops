# syntax=docker/dockerfile:1.9
# Build context is the repository root so evals/ and cli/ ship alongside engine/src.

FROM python:3.14-slim AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

FROM base AS deps
COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock* ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev --frozen --no-install-project 2>/dev/null \
    || uv sync --no-dev --no-install-project

FROM base AS runtime
# Non-root, as mandated. The uid is fixed so the mounted kubeconfig is readable.
RUN groupadd --gid 10001 healops \
    && useradd --uid 10001 --gid healops --create-home --shell /usr/sbin/nologin healops

WORKDIR /app
COPY --from=deps /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/engine/src:/app"

COPY --chown=healops:healops engine/src ./engine/src
COPY --chown=healops:healops engine/alembic.ini ./engine/alembic.ini
COPY --chown=healops:healops engine/migrations ./engine/migrations
COPY --chown=healops:healops policy ./policy
COPY --chown=healops:healops cli ./cli
COPY --chown=healops:healops evals ./evals

USER healops
EXPOSE 8000

# Readiness checks PostgreSQL, Redis, the K8s API and Alertmanager reachability.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz', timeout=3).status==200 else 1)"

# One web server, one port - the Slack AsyncApp is mounted on this FastAPI app.
CMD ["uvicorn", "main:app", "--app-dir", "/app/engine/src", "--host", "0.0.0.0", "--port", "8000"]
