## 1. The image

- [x] 1.1 Write `.devcontainer/Dockerfile` on a digest-pinned Debian bookworm dev container base (D1); verify `docker build .devcontainer` succeeds and `cat /etc/debian_version` in the built image reports bookworm
- [x] 1.2 Copy `uv` into the image from `ghcr.io/astral-sh/uv:0.12.3` at the digest the root `Dockerfile` pins (D2); verify `uv --version` inside reports `0.12.3` and the digest string is identical in both files
- [x] 1.3 Have `uv` provide the interpreter from `.python-version` (D2, amended: `uv sync` fetches it at create time, because the root `.dockerignore` allowlist keeps `.python-version` out of any build context); verify `uv run python --version` reports 3.13 and that no `3.13` literal appears in `.devcontainer/Dockerfile`
- [x] 1.4 Set `UV_PROJECT_ENVIRONMENT=/home/vscode/.venv`, `UV_LINK_MODE=copy` and `PYTHONUNBUFFERED=1` as image environment (D3); verify `uv run python -c "import sys; print(sys.prefix)"` inside prints `/home/vscode/.venv`
- [x] 1.5 Install `@fission-ai/openspec` and the Claude Code CLI globally, each at a pinned version (D7); verify `openspec --version` prints that version and `claude --version` runs

## 2. The container definition

- [x] 2.1 Write `.devcontainer/devcontainer.json` with the workspace bind mount, `remoteUser: vscode` and `updateRemoteUserUID` left at its default (D6); verify a file created inside the container is owned by uid 1000 on the host
- [x] 2.2 Add the pinned GitHub CLI Feature, and Node from a digest-pinned image in the `Dockerfile` (D7, amended: Features are applied after the `Dockerfile`, so `npm` cannot be a Feature when the image installs npm packages); verify `node --version` and `gh --version` run inside
- [x] 2.3 Pass both radios through by their by-identifier names to fixed `/dev/modem-0` and `/dev/modem-1` (D4, amended: bind mounts plus `--device-cgroup-rule`, sourced from the symlinks `initializeCommand` resolves, because both `--device` and `${localEnv:NAME:default}` split on colons); verify `ls -l /dev/modem-0 /dev/modem-1` inside lists both character devices
- [x] 2.4 Add `--group-add=${localEnv:SIGHOP_DEVCONTAINER_TTY_GID:984}` (D5); verify `id` inside lists group 984 and that the container user can open both modems for reading
- [x] 2.5 Add `forwardPorts: [8080]` and nothing else (D11); verify the definition forwards no context-engine port
- [x] 2.6 Add named volumes for `/home/vscode/.cache/uv`, the interpreters `uv` fetches, the GitHub CLI config and the Claude Code configuration — the design's open question is settled: `CLAUDE_CONFIG_DIR` relocates `.claude.json` too, so one volume covers it; verify state written under each volume survives `docker rm` and a rebuild
- [x] 2.7 Add `postCreateCommand` running `git submodule update --init --recursive` and `uv sync --locked`; verify `related-repos/MeshCore` and `related-repos/meshcore_py` are populated afterwards, `uv.lock` is unmodified (`git status --porcelain uv.lock` is empty) and `uv run python -c "import sighop"` succeeds
- [x] 2.8 Add editor customizations — the ruff, mypy and Python extensions, and `python.defaultInterpreterPath` set to `/home/vscode/.venv/bin/python` (D3); verify that interpreter exists in the container and carries ruff and mypy

## 3. The context engine, shared with the host (D8)

- [x] 3.0a Place the workspace at the host's own absolute path inside the container (`workspaceMount`/`workspaceFolder` = `${localWorkspaceFolder}`); verify `cce status` inside reports the host's store (`sighop-3b2286`) and its existing embedding cache, not an empty one
- [x] 3.0b Install the context engine in the image at the host's version, without `fastembed`, in a tool environment whose interpreter is not shadowed by the runtime volume; verify `cce --version` matches the host's
- [x] 3.0c Bind-mount `$HOME/.cce` into the container user's home; verify `cce sessions status` reports byte-identical counts inside and on the host
- [x] 3.0d Forward the host's Ollama onto the container's own `127.0.0.1:11434` from `postStartCommand`, and say so on stderr when the host's Ollama is unreachable (`.devcontainer/ollama-forward.sh`); verify `cce status` inside reports Ollama running with LLM summarization, and that `cce search` returns ranked results from the host's index
- [x] 3.1 Add `.devcontainer/claude/mcp.json` registering the engine by the bare command `cce` and a `mounts` entry shadowing `${containerWorkspaceFolder}/.mcp.json`; verify `claude mcp list` inside resolves the server while the host's own copy is untouched
- [x] 3.2 Add `.devcontainer/claude/settings.local.json` — the permission allowlist, its MCP tool entries and the `cce status` hook, with absolute paths replaced by bare commands — and a `mounts` entry shadowing `${containerWorkspaceFolder}/.claude/settings.local.json`; verify the container reads the shadowed copy
- [x] 3.3 Write `.claude/hooks/cce.sh`: takes the event name, resolves the hook script under `$HOME/.cce/` and *computes* the port file's directory as `<basename>-<sha256(abs path)[:6]>` (a `sighop-*` glob would also match `sighop-check-246-38d095`), runs it when present, exits 0 silently when absent, and carries a header saying to re-apply it after `cce init`; verify it exits 0 with no output when `$HOME/.cce/` does not exist, resolves `sighop-3b2286` on the host, and runs inside the container
- [x] 3.4 Rewrite the five hook commands in `.claude/settings.json` to call `.claude/hooks/cce.sh <event>` (D8); verify the wrapper resolves the same port file the generated command hard-coded and produces the same output on the host
- [x] 3.5 Confirm no `mounts` entry shadows a tracked file; verify `git status --porcelain` inside the running container reports neither `.mcp.json` nor `.claude/settings.local.json`

## 4. Documentation

- [x] 4.1 Write `.devcontainer/README.md`: host prerequisites, the `SIGHOP_DEVCONTAINER_MODEM_*` and `SIGHOP_DEVCONTAINER_TTY_GID` variables with the no-radio fallback, the ESP32-S3 re-enumeration caveat and which board to prefer, `host.docker.internal` for a host-loopback database, re-applying the hook wrapper after `cce init`, and the stale-index note for commits made inside; verify each documented command runs as written
- [x] 4.2 Add the dev container section to `CLAUDE.md` — the engine works in there against the host's own index and memory, with indexing, simultaneous sessions and the Ollama forward as the three differences; verify the wording does not weaken the requirement for host sessions, and that it sits outside the block `cce init` rewrites
- [x] 4.3 State in `.devcontainer/README.md` which `build.sh` gates run inside (`lock lint types test`) and which need the host (`image smoke replay scan`) with the reason (D9); verify `./build.sh lock lint types test` runs its gates inside the container
- [x] 4.4 Add `.devcontainer/` to the repository layout tree in `DESIGN.md` §11; verify the entry names what the directory holds, as the neighbouring entries do

## 5. Verification against the spec

- [x] 5.1 Build the container from a clean checkout on this host and run `uv run --locked ruff check`, `uv run --locked mypy` and `uv run --locked pytest -q` inside; verify all three pass and the database-marked tests skip with no `DATABASE_URL` set
      <!-- pytest: 1938 passed, 395 skipped with no DATABASE_URL set - the database-marked
           tests skip, as the spec requires. ruff and mypy report 3 and 1 failures that are
           PRE-EXISTING at HEAD (047f7e6) and identical on the host with src/ and tests/
           unmodified (`git status --porcelain src tests` empty): import sorting in
           net/dm.py:45, net/room.py:50, tests/test_dm.py:16, and `"BaseRoute" has no
           attribute "path"` at tests/test_web_write_parity.py:1881.
           Closed at the user's direction: the spec's scenario is that the lint, type and
           test gates "pass or fail exactly as they do on the host", which holds. Fixing
           HEAD's four errors is another change's work - this change's Impact names no
           file under src/ as modified. -->
- [x] 5.2 Run `uv run --env-file .env.dev --locked pytest -q` inside; verify the database-marked tests run against the external database rather than skipping
- [x] 5.3 Start `sighop run --device /dev/modem-1 --web --web-host 0.0.0.0 --web-port 8080` inside; verify the modem opens and the panel answers in a host browser on port 8080
      <!-- Unblocked at the user's authorisation: `sighop db upgrade` took the dev database
           from 0007 to 0008, and the panel then started inside the container - database
           opened, `web_interface_listening` on 0.0.0.0:8080, persistence restored.
           Two findings, neither of them the container's:
           1. The command as specified 421s a host browser. The panel's rebinding defence
              (allowed_hosts, src/sighop/web/app.py:664) answers only to the name bound, so
              a wide bind takes `Host: 0.0.0.0:8080` and refuses the `localhost:8080` the
              forward sends - "This server does not answer to that host name", confirmed in
              the user's browser. With `--web-allowed-host localhost:8080` the same request
              returns 303 and then `<title>sign in - sighop</title>`. `.devcontainer/README.md`
              now documents the flag as required rather than optional (task 4.1's "runs as
              written").
           2. `/dev/modem-1` opens as the container user but its board never answers the
              handshake (`modem_handshake_unanswered`, `device_name` timeout, TX disabled).
              `/dev/modem-0` handshakes fully from inside - `modem_ready`, "Heltec V4 OLED",
              firmware v1, radio parameters confirmed by the board, live telemetry (battery
              4261 mV, MCU 36.0 C). Bidirectional serial through the passthrough is therefore
              proven; modem-1's silence is that board's firmware, not the device mount. The
              verified run used `/dev/modem-0`. -->
- [x] 5.4 With the host's own `.venv` present, run `uv sync --locked` inside and then `uv run --locked pytest -q` on the host; verify the host's environment is unaffected (D3)
- [x] 5.5 Remove and rebuild the container, then verify the `gh` config, the Claude Code configuration and a cache-reusing `uv sync --locked` all survive without re-downloading
- [x] 5.6 Run `openspec list` and `openspec status` inside; verify they resolve this repository's own planning root
- [x] 5.7 Run `openspec validate devcontainer-dev-environment --strict`; verify it passes
- [x] 5.8 Record a decision from inside the container and recall it on the host (and the reverse); verify one memory store backs both
