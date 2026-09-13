# syntax=docker/dockerfile:1
#
# The sighop image (DESIGN.md §10, milestone 9 design D13).
#
# Two stages on the SAME digest-pinned base, so the virtual environment's
# interpreter symlink resolves identically in both. The final stage is the base
# image plus one COPY of /app: the installed environment and the migration
# chain. It adds no compiler, no package installer, no source checkout, no
# tests and no secrets, and it runs no commands of its own.
#
# The base's own pip and apk are left in place. Deleting them in a later layer
# saves no bytes — they stay in the base layers — and would only hide them from
# the vulnerability scan while still shipping them. Under the deployment's
# read-only root and non-root user, apk cannot install anything, and the scan
# reports both for what they are.
#
# It sets no USER: the deployment chooses one (compose runs it as the operator's
# UID with the host's dialout GID), and every file is readable by any user.
# Bytecode is compiled here, so a read-only root filesystem needs no writable
# cache at start-up.
#
# The base is python:3.13-alpine (musl). Every native dependency in uv.lock
# ships a musllinux wheel, so nothing compiles; a future one that does not makes
# this build fail, which is the right signal for an image that promises no
# toolchain. build.sh's replay gate runs the corpus in this image and compares
# the output byte for byte with the glibc host's, because the test suite itself
# never runs on musl.
#
# Deviation from §10, recorded in DESIGN.md: busybox's shell remains. Wolfi /
# Chainguard (no shell) was rejected because its free tier publishes `latest`
# only and would move the interpreter under a lock resolved for 3.13; slim was
# replaced because its Debian base carried 56 HIGH/CRITICAL findings to
# Alpine's 7.

ARG PYTHON_IMAGE=python:3.13-alpine@sha256:7415fbc3c9e4979cc717d92377ab2bc7b2b4a2af1ac03cc52b5f3f88efedaf3a
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.3@sha256:2d890623d310b57771ce840f0da5eed5fc6d657da05ffaa45d82797b53fa3abc

FROM ${UV_IMAGE} AS uv

# --- Build: resolve nothing, install exactly what uv.lock says ---------------
FROM ${PYTHON_IMAGE} AS build

COPY --from=uv /uv /usr/local/bin/uv

# --locked, not --frozen: --frozen installs from a stale lock without complaint,
# and a lock that disagrees with pyproject.toml must fail the build.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /src

# The dependency layer, cached across source changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

# The application itself, installed non-editable so nothing points back at /src.
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

# The migration chain beside it, compiled here so a read-only root needs no
# bytecode cache, and every file readable by whatever user the deployment picks.
COPY alembic /app/alembic
COPY alembic.ini /app/alembic.ini
RUN python -m compileall -q /app/alembic \
 && chmod -R a+rX /app

# --- Final: the runtime, the environment, the migration chain ----------------
FROM ${PYTHON_IMAGE}

ARG SIGHOP_VERSION=0.0.0-dev
ARG SIGHOP_COMMIT=unknown

LABEL org.opencontainers.image.title="sighop" \
      org.opencontainers.image.description="A virtualization platform for MeshCore over a KISS modem" \
      org.opencontainers.image.version="${SIGHOP_VERSION}" \
      org.opencontainers.image.revision="${SIGHOP_COMMIT}"

COPY --from=build /app /app

# SIGHOP_COMMIT_HASH is what logging.py stamps on every event. A version and a
# commit are safe to bake in; nothing secret ever arrives as a build argument.
ENV SIGHOP_ALEMBIC_DIR=/app/alembic \
    SIGHOP_COMMIT_HASH=${SIGHOP_COMMIT} \
    PATH=/app/.venv/bin:${PATH} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# No USER, by design (container-image). The entry point is the command, so the
# container's arguments are sighop's, and it runs as PID 1's child under
# compose's `init: true`, which forwards SIGTERM to the platform's own handler.
ENTRYPOINT ["sighop"]
CMD ["--help"]
