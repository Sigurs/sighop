## Why

The development environment is reproducible only by hand. A working checkout today needs a
host that happens to carry Python 3.13, `uv`, an nvm-installed `@fission-ai/openspec`, the
Claude Code CLI, membership in the serial device's group, and a `.venv` built against that
host's C library. Nothing in the repository states any of this, so a second machine — or a
second person — rebuilds it from the shell history of the first. A committed dev container
makes the toolchain a file in the repository, while leaving on the host what genuinely belongs
there: the container-engine gates of `build.sh`, the model server, and the indexing of the
context engine — whose index and memory the container shares rather than duplicates.

## What Changes

- Add a committed dev container: `.devcontainer/devcontainer.json` and its `Dockerfile`,
  a glibc image carrying Python 3.13 and `uv` pinned to the same version the release image
  uses, so the interpreter and resolver inside match what `pyproject.toml` requires and what
  CI runs.
- Install the agent and planning toolchain in the image: Node with `@fission-ai/openspec`,
  the Claude Code CLI, `gh`, and git with submodule support for `related-repos/`.
- Pass through the radios attached to this host by their `/dev/serial/by-id/` names, and
  give the container's user the numeric group that owns them, so `sighop run --device` talks
  to a real modem from inside the container.
- Keep the project's virtual environment out of the bind-mounted workspace: `.venv` lives in
  a container-local volume, so a container `uv sync` can never overwrite the host's `.venv`
  (or be overwritten by it) on a tree the two share.
- Forward the web panel's port so `sighop run --web` is reachable from the host browser, and
  carry `.env.dev` in from the workspace as it already is — the database stays external, no
  database runs in or beside the container.
- Persist the CLI state that a rebuild would otherwise discard — the Claude Code credentials,
  the `gh` login and the `uv` cache — in named volumes.
- Give the container the context engine's tools — `context_search`, `session_recall`,
  `record_decision` — backed by the **host's** index and memory store rather than a second copy:
  `$HOME/.cce` is mounted in, the workspace keeps the host's absolute path (which is what the
  store is keyed by), and embedding and summarisation go to the host's Ollama. Indexing stays on
  the host.
- Document, in the repository, what deliberately does not work inside the container:
  `build.sh`'s `image`, `smoke`, `replay` and `scan` gates need a container engine that the
  dev container is not given, indexing does not happen in there, and a host session and a
  container session should not run at the same time.
- **No change to the release image, `compose.yaml`, `build.sh` or CI.** The dev container is
  additive; the host workflow that exists today keeps working unchanged.

## Capabilities

### New Capabilities
- `dev-container`: what a checkout's committed development container provides — the
  interpreter and toolchain it pins, the host devices and ports it exposes, the state it
  keeps across rebuilds, the host resources it deliberately does not reach, and the
  environment isolation that keeps it from corrupting the host's checkout.

### Modified Capabilities
<!-- None. The dev container adds a way to develop the project; it changes no requirement of
     the platform, the release image, the compose deployment or the build script. -->

## Impact

- **New files**: `.devcontainer/` (`devcontainer.json`, `Dockerfile`, `README.md`,
  `host-devices.sh`, `post-create.sh`, `ollama-forward.sh`, the two shadowed agent config files),
  and `.claude/hooks/cce.sh`.
- **Modified files**: `DESIGN.md` §11 (repository layout), `CLAUDE.md` (how the context engine
  behaves in the dev container), `.claude/settings.json` (its hooks call the wrapper instead of
  one machine's absolute paths).
- **Dependencies**: none added to `pyproject.toml` or `uv.lock`. The dev image pulls its own
  toolchain (Node, the OpenSpec CLI, the Claude Code CLI, the context engine, `gh`); nothing it
  installs reaches the release image, which is built from `Dockerfile` at the repository root and
  is untouched.
- **Host requirements**: a container engine; for the radios, the same `/dev/serial/by-id/` names
  and device group the host uses today; for the context engine, its existing installation and an
  Ollama that listens past loopback.
- **Not affected**: the release `Dockerfile`, `compose.yaml`, `build.sh`, `.github/workflows/`,
  and every spec under `openspec/specs/` that describes the running platform.
