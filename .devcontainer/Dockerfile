# syntax=docker/dockerfile:1
#
# The sighop development container (change devcontainer-dev-environment).
#
# Nothing here ships. This image is the toolchain a checkout guarantees to
# whoever opens it; the image that runs in production is the `Dockerfile` at the
# repository root, and the two share exactly one thing on purpose — the `uv`
# digest below.
#
# Debian, not the release image's Alpine (design D1). The test suite has never
# run on musl: `build.sh`'s `replay` gate exists precisely because the host is
# glibc and the image is not. A musl dev container would change what the suite
# proves without removing the need for that gate.
#
# The build context is this directory, so nothing from the checkout is copied in
# — not the source, not `pyproject.toml`, not `.python-version`. The workspace
# arrives as a bind mount at run time, and `postCreateCommand` installs from it.
# That is also why the interpreter is not installed here: `uv sync` reads the
# workspace's own `.python-version` and fetches the interpreter it names into
# UV_PYTHON_INSTALL_DIR, which the container definition keeps on a volume. One
# source for the version (design D2), and no `3.13` written down a second time.

ARG BASE_IMAGE=mcr.microsoft.com/devcontainers/base:bookworm@sha256:3aacff4130e6cf04709f9cab1d7a6d3e1cc4bff6202bc61611831a18d3755673
# The same version and digest the release Dockerfile pins. Two places already
# agree on the resolver; a third that floated would eventually resolve
# differently from the image (design D2).
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.3@sha256:2d890623d310b57771ce840f0da5eed5fc6d657da05ffaa45d82797b53fa3abc
# Node is copied in rather than added as a dev container Feature: Features are
# applied on top of this image, so `npm` does not exist while it is being built,
# and the two command-line tools below have to be installed with a pinned
# version somewhere. A digest-pinned official image is that somewhere.
ARG NODE_IMAGE=node:24-bookworm-slim@sha256:2fe369e969550cde8e867afc3fe370b260140cab4a23d467074295b42163d553

ARG OPENSPEC_VERSION=1.13.1
ARG CLAUDE_CODE_VERSION=2.1.276
# The context engine. The host's index, its memory database and its hooks are
# bind-mounted in; this is the client that reads them. Keep it at the version the
# host runs: both processes open the same SQLite stores.
ARG CCE_VERSION=0.4.26

FROM ${UV_IMAGE} AS uv
FROM ${NODE_IMAGE} AS node

FROM ${BASE_IMAGE}

COPY --from=uv /uv /usr/local/bin/uv

# socat and netcat forward the host's Ollama onto this container's own
# localhost, which is the only address some of the context engine's code paths
# will look at. See ollama-forward.sh.
RUN apt-get update \
 && apt-get install -y --no-install-recommends socat netcat-openbsd \
 && rm -rf /var/lib/apt/lists/*

# Node's own layout, unmodified: the binary, its library directory and the two
# symlinks into it that npm installs as.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && node --version && npm --version

# `uv` never touches the workspace's own `.venv`: that one belongs to the host,
# which built it against a different C library and a different interpreter, and
# the two share a working tree (design D3).
ENV UV_PROJECT_ENVIRONMENT=/home/vscode/.venv \
    UV_LINK_MODE=copy \
    UV_CACHE_DIR=/home/vscode/.cache/uv \
    UV_PYTHON_INSTALL_DIR=/home/vscode/.local/share/uv/python \
    PYTHONUNBUFFERED=1 \
    PATH=/home/vscode/.venv/bin:${PATH}

ARG OPENSPEC_VERSION
ARG CLAUDE_CODE_VERSION
RUN npm install -g --no-fund --no-audit \
      "@fission-ai/openspec@${OPENSPEC_VERSION}" \
      "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && npm cache clean --force \
 && openspec --version \
 && chmod -R a+rX /usr/local/lib/node_modules

# The context engine, in its own tool environment on the PATH.
#
# UV_PYTHON_INSTALL_DIR is overridden for this one command on purpose: its
# run-time value is a volume that is not mounted while the image is built, so an
# interpreter installed there would be shadowed at run time and this tool would
# lose the interpreter its virtual environment points at.
#
# `fastembed` is deliberately NOT installed. Without it the engine embeds
# through Ollama (nomic-embed-text), which is what the host does — and the two
# share one vector store, so they must embed the same way.
ARG CCE_VERSION
RUN UV_PYTHON_INSTALL_DIR=/opt/uv/python \
    UV_TOOL_DIR=/opt/uv/tools \
    UV_TOOL_BIN_DIR=/usr/local/bin \
    uv tool install --python 3.13 "code-context-engine==${CCE_VERSION}" \
 && chmod -R a+rX /opt/uv \
 && cce --version

# The directories the container definition mounts volumes over. Creating them
# here, owned by the user, is what gives a freshly created volume its ownership:
# Docker seeds an empty volume from the image path it is mounted at.
USER vscode
RUN mkdir -p /home/vscode/.venv \
             /home/vscode/.cache/uv \
             /home/vscode/.local/share/uv/python \
             /home/vscode/.claude \
             /home/vscode/.config/gh \
             /home/vscode/.cce
