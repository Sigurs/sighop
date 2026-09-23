#!/usr/bin/env bash
#
# Run once, when the dev container is created (devcontainer.json).
#
# Two steps, both against the bind-mounted workspace: the submodules the
# repository references, and the environment `uv.lock` describes. `--locked` is
# the same refusal build.sh's `lock` gate makes — a container that quietly
# resolved newer versions than the lock file would produce test results that
# mean nothing.
#
# The environment is built at $UV_PROJECT_ENVIRONMENT (/home/vscode/.venv), not
# at ./.venv: the workspace is shared with the host, whose own .venv was built
# for a different interpreter and C library.

set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> submodules"
git submodule update --init --recursive

echo "==> uv sync --locked"
# The interpreter comes from the workspace's .python-version, which is why none
# is baked into the image (design D2). uv downloads it once into
# $UV_PYTHON_INSTALL_DIR, which is a volume, so later rebuilds reuse it.
uv sync --locked

printf '\n  python   %s\n  uv       %s\n  openspec %s\n  claude   %s\n\n' \
  "$(uv run python --version)" \
  "$(uv --version)" \
  "$(openspec --version)" \
  "$(claude --version)"

cat <<'EOF'
The database is external: nothing here sets DATABASE_URL. Load the checkout's
own .env.dev the way the host does. sighop takes no arguments, so the rest of
its settings go in the environment too, for example

    SIGHOP_MODEM=/dev/modem-1 SIGHOP_WEB_HOST=0.0.0.0 \
      SIGHOP_WEB_ALLOWED_HOSTS=localhost:8080 uv run --env-file .env.dev sighop

The test suite needs that database as well — it refuses to run without one:

    uv run --env-file .env.dev pytest

The context engine works in here: the host's index and memory are mounted, and
embedding and summarisation go to the host's Ollama. Indexing stays on the host.

See .devcontainer/README.md for the radios and for the build.sh gates that need
a container engine, which this container is not given.
EOF
