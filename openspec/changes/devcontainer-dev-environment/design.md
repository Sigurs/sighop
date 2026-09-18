## Context

See proposal.md — Why. The constraints the design has to work around are all properties of the
checkout as it stands today:

- The release image is Alpine/musl and digest-pinned (`Dockerfile`); `build.sh` runs eight gates,
  four of which (`image`, `smoke`, `replay`, `scan`) drive a container engine.
- `pyproject.toml` requires Python `>=3.13`, lints for `py313`, and `.python-version` says `3.13`.
  `uv.lock` is authoritative — `build.sh`'s first gate fails on a lock that disagrees with it.
- The host's radios are `root:uucp 660` with group id **984**, reached through
  `/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_90:70:69:85:AD:28-if00` (ttyACM0)
  and `/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0`
  (ttyUSB0).
- `.env.dev` (gitignored) holds `DATABASE_URL` for an external Postgres at `172.20.4.20:30432`
  and `SIGHOP_SECRET_KEY`; nothing loads it automatically — `uv run --env-file .env.dev` does.
- The context engine is a host installation: `cce` under `~/.local/share/uv/tools/`, an index
  under `~/.cce/projects/`, and Ollama on `localhost:11434`. Three things in the checkout point
  at it by absolute host path: `.mcp.json` (gitignored), `.claude/settings.json` (**tracked** —
  five hook entries naming `/home/sigurs/.cce/hooks/cce_hook.sh`), and `.claude/settings.local.json`
  (gitignored). The git hooks `post-checkout`, `post-commit` and `post-merge` also call `cce`,
  backgrounded with output discarded.
- `openspec` is the npm package `@fission-ai/openspec`, installed on the host through nvm.

The user chose: context engine stays on the host; radios passed through by their by-identifier
names; database external from `.env.dev`; Claude Code, OpenSpec and git tooling in the image.
Not choosing the "trivy + docker CLI" tooling option is read here as **no container engine inside
the container**; §Decisions D9 records what that costs.

## Goals / Non-Goals

**Goals:**

- One committed definition that builds on any host with a container engine, with host-specific
  values (device names, device group) supplied by that host's environment rather than by editing
  a tracked file.
- An environment inside the container that cannot damage the host's checkout — in particular its
  virtual environment — while sharing the same working tree.
- The absolute host paths that point at the context engine fail quietly, or not at all, inside the
  container: no session that starts with five broken hooks, and no working tree that reads as
  dirty because a mount shadowed a tracked file.

**Non-Goals:**

- Running the platform's production image, `compose.yaml`, or CI inside the dev container.
- Reproducing the musl runtime: the dev container is glibc, like the host and like CI. The one
  place musl behaviour is checked stays `build.sh`'s `replay` gate.
- Making `build.sh` fully runnable inside the container (D9), or running Postgres, Ollama or the
  context engine anywhere near it.
- Multi-architecture support. The definition builds for the host's architecture, as `build.sh` does.

## Decisions

### D1. Debian (glibc), not the release image's Alpine
The dev container is built on a digest-pinned Debian base, not on `python:3.13-alpine`.

*Why*: the test suite has never run on musl — `build.sh`'s `replay` gate exists precisely because
the host is glibc and the image is not. A musl dev container would change what the test suite
proves without removing the need for that gate, and would make every wheel a source build.

*Alternative rejected*: sharing the release `Dockerfile`'s base for fidelity. Fidelity is not
what a dev container is for here; the `replay` gate already owns that question.

### D2. `uv` pinned to the release image's version; the interpreter installed by `uv`
The image copies `uv` from `ghcr.io/astral-sh/uv:0.12.3` at the same digest `Dockerfile` pins, and
the interpreter comes from `.python-version` by way of `uv`.

*Amended during implementation*: the interpreter is fetched when the container is **created**, by
`uv sync` in `postCreateCommand`, not while the image is built. The root `.dockerignore` is an
allowlist that excludes `.python-version`, so a build with the repository as its context cannot
read the file, and a build with `.devcontainer/` as its context cannot reach it at all. Fetching it
at create time reads the file from the bind-mounted workspace instead, keeps `.python-version` the
only place the version is written, and costs one download — into
`UV_PYTHON_INSTALL_DIR`, which is a volume, so later rebuilds reuse it (measured: a rebuild after
`docker rm` took ten seconds and downloaded nothing).

*Why*: two places already agree on the resolver version; a third that floats would eventually
resolve differently from the image. Letting `uv` provide the interpreter keeps `.python-version`
the single source of the interpreter version, rather than duplicating `3.13` in a base image tag.

*Alternative rejected*: `mcr.microsoft.com/devcontainers/python:3.13-bookworm`. Its tag floats,
and it would put the interpreter's version in a second place that has to be kept in step by hand.

### D3. The virtual environment lives outside the workspace, via `UV_PROJECT_ENVIRONMENT`
The image sets `UV_PROJECT_ENVIRONMENT=/home/vscode/.venv`, so `uv sync` and `uv run` inside the
container never touch the bind-mounted `.venv`.

*Why*: the host's `.venv` is built for the host's C library and interpreter. A shared working tree
means one `uv sync` in the wrong place silently replaces the other side's environment — a failure
that looks like a broken interpreter, not like a mistake. An environment variable does this with
no volume, no ownership seeding, and no first-run repair step.

*Cost*: the editor's interpreter path is absolute and outside the workspace, so
`python.defaultInterpreterPath` is set explicitly in the container's editor settings.

*Alternative rejected*: a named volume mounted over `${containerWorkspaceFolder}/.venv`. It keeps
the conventional path, but a freshly created volume takes its ownership from the image's directory,
which has to be seeded in the `Dockerfile` — a subtlety that breaks silently and confusingly when
it is wrong.

### D4. Radios are addressed by by-identifier name on the host and by a fixed name inside
Commands inside the container always say `/dev/modem-0` or `/dev/modem-1`, whatever the host calls
the device. A host with different radios (or none) says so in its own environment rather than
editing a tracked file.

*Amended during implementation, twice, for the same reason — colons:*

1. `--device=<src>:<dst>` cannot carry a by-id name that contains a colon, and this host's ESP32-S3
   carries a MAC address in its name. The radios are therefore **bind-mounted** (`mounts` takes
   comma-separated `key=value`, where a colon is just a character) and made openable by two
   `--device-cgroup-rule` entries, `c 166:* rmw` for ttyACM and `c 188:* rmw` for ttyUSB. Without
   the rules the device node appears and `open()` returns `EPERM`; with them, both radios open from
   inside as the container user.
2. `${localEnv:NAME:default}` is **split at the first colon of the default**, which silently
   truncated the path to `…usb-Espressif_USB_JTAG_serial_debug_unit_90` — a path that does not
   exist, and a container that refused to start. So the names moved out of `devcontainer.json`
   into `.devcontainer/host-devices.sh`, run by `initializeCommand` on the host, which resolves
   `$SIGHOP_DEVCONTAINER_MODEM_0` / `_1` (else this host's committed by-id defaults, else
   `/dev/null`) into two colon-free symlinks under `$HOME/.cache/sighop-devcontainer/`. Those are
   what the mounts name.

A welcome side effect of (2): a host with no radio attached needs no configuration at all. The
script says on stderr that it is using `/dev/null`, and the container starts.

*Why*: this is `compose.yaml`'s existing pattern — a by-id source, a fixed target (`/dev/modem`),
host-specific values in the host's own environment — and by-id names survive re-enumeration where
`ttyUSB0` does not.

*Cost*: the committed defaults name this host's radios, so another host that starts the container
without setting the variables fails at start with a missing device. That is a start-time failure
with a clear message, not a build-time one, and the requirement that the definition build anywhere
is unaffected.

### D5. The device group is a numeric group id from the host's environment
`--group-add=${localEnv:SIGHOP_DEVCONTAINER_TTY_GID:984}`.

*Why*: the host's radio group is `uucp`=984 (Arch); Debian's `uucp` is 10 and its `dialout` is 20.
A group *name* would resolve against the image's `/etc/group` and grant access to the wrong group —
the same trap `compose.yaml` already documents for `DIALOUT_GID`.

### D6. The container user is the `vscode` user, remapped to the host's uid
The definition keeps `updateRemoteUserUID` at its default, so the non-root user inside is remapped
to the host user that owns the checkout (1000 here).

*Why*: the working tree is a bind mount. Without the remap, everything written inside is owned by
the image's uid and needs repair on the host.

### D7. Agent and planning tooling is installed by pinned version
`@fission-ai/openspec` and the Claude Code CLI installed globally at pinned versions in the
`Dockerfile`; the GitHub CLI from a pinned Feature. Submodules
(`related-repos/MeshCore`, `related-repos/meshcore_py`) are populated in `postCreateCommand`
together with `uv sync --locked`.

*Why*: `--locked` is the same refusal `build.sh`'s `lock` gate makes — a container that quietly
resolved newer versions than `uv.lock` would produce test results that mean nothing.

*Amended during implementation*: Node is **not** a Feature. Features are applied on top of the
image, so `npm` does not exist while the `Dockerfile` runs, and the two tools above have to be
installed with a pinned version somewhere. Node is copied from a digest-pinned official
`node:24-bookworm-slim` instead, which keeps the whole image buildable by plain `docker build`.
The GitHub CLI stays a Feature: it needs nothing from our build.

### D8. The container shares the host's context engine; the three pointers at it are fixed, not removed
**Revised after the first implementation pass, at the user's direction.** The container was going
to carry no context engine at all. It now carries the client and *shares the host's state*: one
index, one memory database, one model server. Four things make that work, and each was found by
trying it:

- **The workspace sits at the host's own absolute path inside the container**
  (`workspaceMount` / `workspaceFolder` = `${localWorkspaceFolder}`, not `/workspaces/sighop`).
  The engine keys its store `<basename>-<sha256(absolute path)[:6]>`, so any other path opens a
  different, empty index and says nothing about it. This is the whole reason the conventional
  path is abandoned.
- **`$HOME/.cce` is bind-mounted** to the container user's home. Vector store, FTS index,
  `memory.db` and the hook script are then the same files, SQLite in WAL mode over a local
  filesystem, with two processes at most.
- **Embedding goes to the host's Ollama, and `fastembed` is deliberately not installed**, because
  the host has no fastembed either and embeds through Ollama's `nomic-embed-text`. A container
  that embedded locally would write differently shaped vectors into a store the host reads.
- **The host's Ollama is forwarded onto the container's own `127.0.0.1:11434`** rather than
  addressed as `host.docker.internal`. `CCE_OLLAMA_URL` is not enough: in 0.4.26 only
  `resolve_ollama_url()` honours it, and the search path builds its embedding backend from
  `config.ollama_url` directly — so `cce status` reported the host's Ollama as running while
  `cce search` failed with "Start an Ollama server at http://localhost:11434". The alternative,
  a container-local `~/.cce/config.yaml` with a different URL, was rejected: that file is shared
  with the host, where `localhost` is the correct answer, and a second copy is a second thing to
  keep in step.

Indexing stays on the host, whose git hooks run it. Agreed cost: a commit made inside the
container leaves the index stale until the host's next commit or checkout.

Accepted collision: both sides run `cce serve`, and both write `serve.port` into the shared
project directory, so two simultaneous sessions fight over it. The hook probes
`127.0.0.1:<port>` in its own network namespace and gives up quietly, so the failure mode is one
side's memory capture stopping — not a damaged store. Documented rather than solved.

What remains of the original decision is the file-level part, and its logic is unchanged: the
checkout points at the engine from three files, one of which is tracked.

1. **`.mcp.json`** (gitignored): shadowed by a bind mount of a committed
   `.devcontainer/claude/mcp.json`, which registers the same server by the bare name `cce` —
   the host file names `/home/sigurs/.local/bin/cce`, and the engine's `serve` defaults its
   project directory to the working directory, which is the workspace. Shadowing a gitignored
   path cannot make the tree dirty.
2. **`.claude/settings.local.json`** (gitignored): likewise shadowed, by a committed copy that
   keeps the permission allowlist, its MCP tool entries and the `cce status` hook, with the
   absolute path replaced by the bare command.
3. **`.claude/settings.json`** (**tracked**): *not* shadowed. Its five hook entries are rewritten
   to call one committed wrapper, `.claude/hooks/cce.sh <event>`, which runs `cce_hook.sh` when
   it is present and exits 0 silently when it is not, resolving both the hook and the port file
   under `$HOME/.cce/` rather than a baked-in `/home/sigurs/...`. The port file's directory is
   *computed* (`<basename>-<sha256(abs path)[:6]>`, with `sha256sum`), not globbed: this host's
   store directory sits beside `sighop-check-246-38d095`, which a `sighop-*` glob would match.

*Why not shadow all three*: a bind mount over a tracked file makes git inside the container report
that file as modified — a permanently dirty tree, which `build.sh` would stamp into an image tag as
`-dirty`. The wrapper costs one committed script and makes the checkout work on the host, in the
container, and on a machine with no context engine at all.

*Cost*: `cce init` regenerates `.claude/settings.json` with absolute paths, undoing the wrapper.
The wrapper's header and `.devcontainer/README.md` both say to re-apply it after `cce init`.

*Alternative rejected*: installing a no-op `cce` shim inside the image at the host's absolute path.
It works, and it bakes one developer's home directory into a committed image definition.

### D9. No container engine inside the container
No Docker socket is mounted and no Docker CLI is installed, so `build.sh`'s `image`, `smoke`,
`replay` and `scan` gates do not run inside. `lock`, `lint`, `types` and `test` do. `build.sh`
already supports naming gates (`./build.sh lock lint types test`) and says loudly that a partial
run verified nothing, so nothing needs to change in the script.

*Why*: mounting the host's Docker socket gives anything inside the container root on the host, and
the user's tooling choice did not include the Docker or trivy toolchain. The image gates are a
release step, and releases happen on the host and in CI.

### D10. The database stays external and unconfigured by the container
No Postgres service, no `DATABASE_URL` in the definition. `.env.dev` arrives with the workspace and
`uv run --env-file .env.dev …` works unchanged.

*Why*: it is what the host does today, and `.env.dev` is per-host by design. The container reaches
`172.20.4.20:30432` through the engine's normal outbound networking.

*Cost*: a database on the host's own loopback would need `host.docker.internal`, not `localhost`.
Documented in `.devcontainer/README.md`.

### D11. Ports: the web panel out, the host's Ollama in
`forwardPorts: [8080]` — the panel's default — is the only port published *out* of the container.
In the other direction, `postStartCommand` forwards the container's `127.0.0.1:11434` to the
host's Ollama (D8). The engine's dashboard (8765) stays host-side and is opened in a host browser,
not through the container.

## Risks / Trade-offs

- **The ESP32-S3 board's native USB re-enumerates on reset or flash** → `--device` is bound once at
  container start, so that radio disappears inside the container until it is restarted. The CP2102
  board (a separate USB-serial chip) does not do this; `.devcontainer/README.md` names it as the one
  to use for long in-container sessions. `--privileged` with `/dev` mounted wholesale would avoid
  it and is rejected: a root-equivalent container for a hot-plug convenience.
- **A host with no radio attached cannot start the container** until it sets the device variables
  (D4) → the README gives the `/dev/null` line to paste.
- **Commits made inside the container leave the index stale**: the `post-commit` hook calls the
  host's absolute `cce` path, which does not exist inside; it is backgrounded with output
  discarded, so the commit itself succeeds and nothing fails loudly. The host catches up on its
  next commit or checkout, or immediately with `cce index` — which now also works from inside,
  since it writes the same store. Documented rather than solved.
- **Two sessions, one `serve.port`** (D8) → run a session on one side at a time; the failure mode
  is quiet, and it is memory capture, not search.
- **A shared SQLite store written from two namespaces**: WAL over a bind-mounted local filesystem,
  same kernel, at most two writers. Locking is POSIX and holds. It would not over a network
  filesystem, which is worth remembering if the checkout ever lives on one.
- **`cce init` undoes D8's wrapper** → stated in the wrapper's header and the README; the wrapper
  is one file to restore.
- **The conventional `/workspaces/<name>` path is gone** (D8): the workspace is at the host's own
  path inside the container. Anything that assumed the conventional path — a hard-coded
  `/workspaces/sighop` in an editor setting or a script — would break. Nothing in this repository
  assumes it.
- **`CLAUDE.md` describes the engine as a hard requirement** → it gains a section saying the dev
  container shares the host's engine, and naming the three things that differ there (indexing,
  simultaneous sessions, the Ollama forward).
- **Two toolchains for one tree**: the same checkout is now developed from the host and from the
  container. D3 keeps the virtual environments apart, but caches that live in the tree
  (`.ruff_cache/`, `.mypy_cache/`, `.pytest_cache/`) are shared. They are content-keyed and
  disposable; a confusing cache is deleted, not diagnosed.

## Migration Plan

Additive, and reversible file by file. New: `.devcontainer/` (definition, `Dockerfile`, README,
`claude/` shadow files) and `.claude/hooks/cce.sh`. Modified: `.claude/settings.json` (hook
commands → the wrapper), `CLAUDE.md` (the dev container exception), `DESIGN.md` §11 (layout).

Rollback: delete `.devcontainer/`; restore `.claude/settings.json` with `cce init` or from git. No
runtime artifact, no dependency and no CI step is touched, so nothing that ships can regress.

## Open Questions

- ~~Whether the Claude Code CLI relocates **both** `~/.claude/` and `~/.claude.json` under
  `CLAUDE_CONFIG_DIR`.~~ **Settled during implementation: it relocates both.** With
  `CLAUDE_CONFIG_DIR=/home/vscode/.claude`, the CLI wrote its configuration to
  `/home/vscode/.claude/.claude.json` and nothing to `/home/vscode/.claude.json`, so the single
  volume covers it. Verified across a `docker rm` and a rebuild.
