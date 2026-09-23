# The sighop dev container

Everything needed to develop sighop, as a file in the repository rather than a
host reconstructed from shell history: Python (the version `.python-version`
names), `uv` at the version the release image pins, `openspec`, the Claude Code
CLI, `gh`, `git`, and the host's radios.

Open the folder in a dev-container-aware editor, or from a terminal:

```bash
npx @devcontainers/cli up --workspace-folder .
npx @devcontainers/cli exec --workspace-folder . bash
```

## What the host has to provide

A container engine, and — for the radios — the same device group the host uses
for serial devices. Nothing else: no Python, no `uv`, no Node.

The container is built for the host's architecture, as `build.sh` is.

## The radios

The two radios attached to the machine this was written on are the committed
defaults, addressed by their stable by-identifier names and presented inside
under fixed names, so commands in here never change:

| inside        | host (default)                                                       |
| ------------- | -------------------------------------------------------------------- |
| `/dev/modem-0` | `usb-Espressif_USB_JTAG_serial_debug_unit_90:70:69:85:AD:28-if00` (ESP32-S3, native USB) |
| `/dev/modem-1` | `usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0` (USB-serial bridge) |

A different host names its own, in its own environment — no tracked file is
edited:

```bash
export SIGHOP_DEVCONTAINER_MODEM_0=/dev/serial/by-id/usb-...
export SIGHOP_DEVCONTAINER_MODEM_1=/dev/serial/by-id/usb-...
export SIGHOP_DEVCONTAINER_TTY_GID=$(stat -c %g /dev/serial/by-id/usb-...)
```

`SIGHOP_DEVCONTAINER_TTY_GID` is numeric on purpose: a group *name* would
resolve against the image's `/etc/group` instead of the host's, which is the
same trap `compose.yaml` documents for `DIALOUT_GID`. The default, 984, is
`uucp` on an Arch host.

**No radio attached?** Point both at a device that always exists, or the
container will not start:

```bash
export SIGHOP_DEVCONTAINER_MODEM_0=/dev/null
export SIGHOP_DEVCONTAINER_MODEM_1=/dev/null
```

**Prefer `/dev/modem-1` for long sessions.** The ESP32-S3 board speaks USB
itself, so resetting or flashing it makes the device disappear and come back as
a new one; the device is bound once, when the container starts, so `/dev/modem-0`
goes stale until the container is restarted. The CP2102 board is a separate
USB-serial chip and survives a board reset.

## The database

External, always. Nothing in here sets `DATABASE_URL`, and no Postgres runs in
or beside the container — same as the host workflow. The checkout's gitignored
`.env.dev` comes along with the workspace. `sighop` takes no arguments; every
setting is an environment variable (`.env.example` lists them all):

```bash
SIGHOP_MODEM=/dev/modem-1 SIGHOP_WEB_HOST=0.0.0.0 \
  SIGHOP_WEB_ALLOWED_HOSTS=localhost:8080 uv run --env-file .env.dev sighop
```

Port 8080 is forwarded, so that panel answers at <http://localhost:8080> on the
host. Bind the panel to `0.0.0.0` inside; `127.0.0.1` would be the container's
own loopback.

`SIGHOP_WEB_ALLOWED_HOSTS=localhost:8080` is what makes that URL work, and it is
not optional here. The panel's rebinding defence answers only to the name it was
bound to, so a wide bind accepts `Host: 0.0.0.0:8080` and refuses everything
else — including the `localhost:8080` a host browser sends through the forward,
which comes back as `421` and *This server does not answer to that host name*.
The variable extends that set; it never replaces it.

### Tests

The suite needs the same database, and refuses to run without one rather than
skip the half of itself that touches it:

```bash
uv run --env-file .env.dev pytest
```

Each run creates a `sighop_test_<random>` schema inside that database, applies
the real migration chain into it, and drops it when the run ends; a run that was
killed leaves its schema for the next run to sweep up. The suite never starts a
database of its own — there is no Docker socket in here to start one with.

A database on the *host's* loopback is `host.docker.internal`, not `localhost`.

## The build script

`build.sh` takes gate names, and the four that drive a container engine are not
available in here — no Docker socket is mounted and no Docker CLI is installed,
because mounting that socket hands anything in the container root on the host:

```bash
./build.sh lock format lint types test     # in here
./build.sh                                 # on the host: adds image, smoke, replay, scan
```

The `test` gate needs a database like the suite does. With none named in the
environment it uses the one `.env.dev` names, and says so.

The script says loudly that a partial run verified nothing, which is correct: an
image that was never built, smoke-tested, replayed on musl or scanned has not
passed those gates. Releases happen on the host and in CI.

## The context engine

`context_search`, `session_recall` and `record_decision` all work in here. The
engine is installed in the image, and it reads and writes **the host's** index
and memory — one store, not a copy:

- `$HOME/.cce` is bind-mounted to `/home/vscode/.cce`, so the vector store, the
  FTS index, `memory.db` and the hooks are the same files the host uses.
- **The workspace sits at the same absolute path inside as outside** (not
  `/workspaces/sighop`). The engine keys its store by
  `<basename>-<sha256(absolute path)[:6]>`, so a different path in here would
  open a different, empty index without saying so.
- Embedding and summarisation go to the host's Ollama. `postStartCommand`
  forwards `127.0.0.1:11434` in the container to `host.docker.internal:11434`,
  because the engine's search path reads `config.ollama_url` and never sees
  `CCE_OLLAMA_URL` (0.4.26). The host's Ollama must listen past loopback —
  `ss -ltn | grep 11434` on the host shows whether it does.
- `fastembed` is deliberately not installed, so the container embeds through
  Ollama exactly as the host does. Installing it would write differently shaped
  vectors into a store the host also reads.

Indexing stays on the host: its git hooks call `cce index` after a commit,
checkout or merge, and the container has no such hooks. A commit made inside
therefore leaves the index a little stale until the host's next commit or
checkout, or an explicit `cce index` — which also works from in here, since it
writes the same store.

**One thing to avoid: a Claude Code session on the host and one in the container
at the same time.** Both run `cce serve`, and both write `serve.port` into the
shared project directory. The hook probes `127.0.0.1:<that port>` in its own
network namespace and gives up quietly when nothing answers, so the cost is
memory capture silently stopping for one side — not a corrupted store.

Two files name absolute host paths, and the container shadows each with a copy
from `.devcontainer/claude/`: `.mcp.json` (which points at the host's `cce`
binary; the copy just says `cce`) and `.claude/settings.local.json`. Both are
gitignored, which is why they *can* be shadowed — a bind mount over a tracked
file would leave the working tree permanently modified inside the container, and
`build.sh` stamps a modified tree into the image tag as `-dirty`.

The tracked `.claude/settings.json` is not shadowed. Its hooks call
`.claude/hooks/cce.sh`, which resolves the engine's hook and port file under
`$HOME` — right on both sides — and exits 0 in silence where there is no engine
at all. **`cce init` regenerates that file with absolute paths and undoes this**
— point the five hook commands back at the wrapper afterwards.

## Two toolchains, one working tree

The checkout is a bind mount, shared with the host. The virtual environment is
not: inside, `UV_PROJECT_ENVIRONMENT` points at `/home/vscode/.venv`, so
`uv sync` in here never replaces the host's `./.venv`, which was built for a
different interpreter and C library — and the host's `uv sync` never replaces
this one.

The caches that do live in the tree (`.ruff_cache/`, `.mypy_cache/`,
`.pytest_cache/`) are shared. They are content-keyed and disposable; delete one
rather than diagnosing it.

Across a rebuild the container keeps the `uv` download cache, the interpreters
`uv` fetched, the Claude Code login and the `gh` login, each on its own named
volume. None of that is ever written into the checkout.
