# syntax=docker/dockerfile:1.7
#
# Multi-stage build: dependencies are resolved strictly from uv.lock in a builder stage and
# the project (src layout, package `app`) is installed into the venv as a regular wheel.
# The runtime stage gets only that virtualenv and runs as non-root.
# Base images are pinned by tag *and* digest for reproducible builds.

ARG PYTHON_IMAGE=python:3.13.15-slim-trixie@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.4@sha256:9f130421e930bfbdc563c00740466603f8723158e90297d07957c2294640262b

FROM ${UV_IMAGE} AS uv

# --------------------------------------------------------------------------- builder
FROM ${PYTHON_IMAGE} AS builder

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /build

# Optional `ca_bundle` secret: a PEM bundle for networks with a TLS-intercepting proxy
# (`docker build --secret id=ca_bundle,src=ca.pem .`). It is never stored in a layer.

# 1) Dependencies only: this layer is reused until pyproject.toml or uv.lock change.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=secret,id=ca_bundle,required=false \
    if [ -s /run/secrets/ca_bundle ]; then export SSL_CERT_FILE=/run/secrets/ca_bundle; fi \
    && uv sync --frozen --no-dev --no-install-project

# 2) The project itself, non-editable so the venv is self-contained.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=secret,id=ca_bundle,required=false \
    if [ -s /run/secrets/ca_bundle ]; then export SSL_CERT_FILE=/run/secrets/ca_bundle; fi \
    && uv sync --frozen --no-dev --no-editable

# --------------------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE} AS runtime

ARG APP_UID=10001
ARG APP_GID=10001

RUN groupadd --system --gid "${APP_GID}" app \
    && useradd --system --uid "${APP_UID}" --gid app --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown app:app /data

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_ENV=prod \
    DB_PATH=/data/app.db \
    # matplotlib needs a writable cache dir; /tmp is the only writable path besides /data.
    MPLCONFIGDIR=/tmp/matplotlib

COPY --from=builder /opt/venv /opt/venv
# Migrations ship with the image: the entrypoint runs `alembic upgrade head` before the app.
COPY alembic.ini /app/alembic.ini
COPY alembic /app/alembic
# One-off tools run on the server (F0 probe, history import, backtest): RUNBOOK.md.
COPY scripts /app/scripts
COPY --chmod=0755 deploy/entrypoint.sh /usr/local/bin/entrypoint.sh

WORKDIR /data
USER app:app

# `python -m app.ops.healthcheck`: database reachable, scheduler tick fresh (real services),
# disk space. start-period covers migrations and startup.
HEALTHCHECK --interval=60s --timeout=15s --start-period=90s --retries=3     CMD ["python", "-m", "app.ops.healthcheck"]

# The entrypoint migrates, then runs the app as a child and forwards SIGTERM to it (graceful
# shutdown: polling stops, running scheduler jobs get up to 60 s). Exit codes: see README.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "app.main"]
