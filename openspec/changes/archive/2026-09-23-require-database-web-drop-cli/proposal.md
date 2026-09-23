# Proposal

## Why

sighop carries three surfaces for every operation — a command, a panel route, and a runtime path —
and two configurations of itself — with a database and without, with the web and without. The
command surface is `cli.py`, 4288 lines, the largest file in the project, plus roughly 3000 lines
of tests that exercise it. The optional database is a second code path through the runtime, the
panel and nine capability specs: every store carries a `persistence is None` branch, every page
carries a "no database is configured" state, and every spec carries a scenario for a node that
stores nothing.

None of that optionality is deployed. `compose.yaml` — the only deployment — always sets
`DATABASE_URL` and always passes `--web`. The panel has covered the command surface since the write
parity work of milestone 8. The result is a large amount of code, tests and specification kept alive
to describe configurations nobody runs.

## What Changes

- **BREAKING** — the `sighop` command line is removed entirely. `build_parser` and every
  subcommand go: `capture`, `monitor`, `run`, `keys`, `room`, `bot`, `webhook`, `channel`,
  `web user`, `database`. The console script becomes an entry point that boots the node and
  nothing else, with no arguments to parse.
- **BREAKING** — every `run` flag becomes an environment variable. `--device` becomes
  `SIGHOP_MODEM`, `--enable-transmit` becomes `SIGHOP_ENABLE_TRANSMIT`, `--duty-cycle-ceiling`
  becomes `SIGHOP_DUTY_CYCLE_CEILING`, and so on for the remainder. `compose.yaml` loses its
  `command:` block for an `environment:` block.
- **BREAKING** — a database is required. `DATABASE_URL` unset is a startup failure, not a mode.
  Every `persistence is None` branch in the runtime, every "no database" state in the panel, and
  every no-database scenario in the specs is deleted.
- **BREAKING** — the web interface is always served. `--web` and its off-by-default behaviour go;
  the bind address, port and allowed host names come from the environment.
- **BREAKING** — `SIGHOP_SECRET_KEY` is required. Unset, the node prints the `openssl` command that
  generates one and exits non-zero, rather than starting in a state where nothing can be sealed.
- **BREAKING** — operator account management is lost. `sighop web user add|list|passwd|disable|
  enable|remove` has no panel equivalent, and this change does not add one (see Risks). The account
  created at `/setup` is the only account, and its password cannot be changed.
- **BREAKING** — a stored channel pre-shared key can no longer be read back. `sighop channel key`
  was the only surface that printed one, and the panel has never rendered a key by design. An
  operator must keep the key they supplied (see Risks).
- Migrations are applied on every start. `--migrate` stops being an opt-in and `database upgrade`
  stops existing; the container already runs with `--migrate` in the only deployment there is.
- **BREAKING** — tests always run against a real PostgreSQL named by `SIGHOP_TEST_DATABASE_URL` or
  `DATABASE_URL`. The `database` marker, its 516 decorators, the collection-time skip and
  `SKIP_REASON` are deleted; a run that configures no database stops before collection with one
  message naming both variables. Each run still works in a `sighop_test_<random>` schema it drops
  afterwards, so concurrent runs do not collide.
- A replay-only module entry point (`python -m sighop.replay`) is added, used by nothing but
  `build.sh`'s parity gate. It is not a command line: one positional capture path, no flags.
- `build.sh`'s `smoke` gate becomes an import check plus a start-and-refuse check; its `replay`
  gate drives the new module entry point instead of `sighop run --replay`.

## Capabilities

### New Capabilities

- `node-boot`: the entry point that boots the node — what it reads from the environment, what it
  refuses to start without, that it migrates before it serves, that it always serves the web
  interface, and what it reports at startup. It carries forward the runtime behaviours that
  `runtime-cli` held and that survive the command line's removal: the periodic status report, the
  rooms, bots, channels and webhooks a run serves and says it is serving, and the rule that
  rendered output never presents unverified content as verified.

### Modified Capabilities

- `runtime-cli`: retired. Every requirement is removed; the run behaviours worth keeping move to
  `node-boot` and the command surfaces are gone.
- `capture-cli`: retired. Nothing records new captures; the committed corpus stays readable.
- `monitor-cli`: retired. The panel is the live view.
- `capture-replay`: gains the replay-only module entry point the build's parity gate drives.
- `database`: the database stops being optional, and tests stop being allowed to run without one.
- `web-server`: the interface stops being opt-in, and its address comes from the environment.
- `web-auth`: accounts stop being managed from the terminal; first-run setup is the only way an
  account comes to exist.
- `container-image`: the entry point takes no arguments and the image applies migrations by
  starting, not by being given a command.
- `compose-deployment`: configuration moves from `command:` to `environment:`.
- `build-script`: the smoke gate stops running `--help` and the parity gate stops running
  `sighop run --replay`.
- `path-hash-size`: its scenarios stop being about `sighop run` and become about the node starting.
- `contacts`, `path-learning`, `channel-store`, `room-server`, `bot-runtime`, `webhooks`: each
  loses its no-database requirement text and scenario; durability stops being conditional.
  `channel-store` also gains the rule that a stored pre-shared key is never read back.
- `web-chat`, `web-admin`, `web-dashboard`: each loses its "no database is configured" degraded
  state. Degrading when a database is present but unreachable is unchanged.
- `dev-container`: the suite no longer skips when no database URL is configured.

## Impact

**Deleted**: `src/sighop/cli.py` (4288 lines), `src/sighop/monitor/run.py`, and the parts of
`src/sighop/monitor/render.py` that only the monitor command called. `tests/test_bot_cli.py`,
`tests/test_room_cli.py`, `tests/test_channel_cli.py`, `tests/test_webhook_cli.py`,
`tests/test_web_user_cli.py`, `tests/test_monitor.py` (~2900 lines).

**Rewritten**: `tests/test_web_write_parity.py` loses the command half of every parity pair — the
panel writes stay, the assertion that they are the same call as the command's goes.
`tests/test_entity_store.py`, `tests/test_first_transmit.py`, `tests/test_runtime.py`,
`tests/test_runtime_persistence.py`, `tests/test_web_server.py` drive `cli.main` today and must
drive the runtime directly.

**Moved, not deleted**: `open_persistence`, `migrate_on_start`, `_attach_web`, `_run_live`,
`_run_replay` and `_database_config` are boot logic that lives in `cli.py` today. They move to a
new `src/sighop/boot.py` rather than dying with the parser.

**Kept**: `src/sighop/radio/capture.py` and `src/sighop/radio/replay.py` — the runtime writes
captures and the test suite and the parity gate read them; only the commands around them go.
`src/sighop/passwords.py` and the account repository stay for `/setup` and `/login`.

**Configuration**: `compose.yaml`, `.env.example`, `.devcontainer/post-create.sh`,
`.devcontainer/README.md`, `Dockerfile` (`CMD ["--help"]` goes), `build.sh`, `DESIGN.md` §10 and
§11, and `pyproject.toml` (`[project.scripts]` and the `database` marker).

**Risks**:

- *No account recovery.* After this change an operator who forgets the `/setup` password has no way
  back in and no way to add a second operator: `/setup` closes once an account exists. This is
  accepted deliberately and should be closed by a follow-up change that adds account routes to the
  panel.
- *No way to read back a channel's pre-shared key.* Giving a private channel to someone new now
  depends on the operator having kept the key they pasted in. Accepted deliberately; the panel says
  so where the key is added and where channels are listed.
- *No way to record new captures.* With `capture-cli` retired the committed corpus is fixed. The
  runtime's own capture writing is kept, so a future change can expose it through the environment.
- *A reachable PostgreSQL becomes a test prerequisite.* A developer without one can no longer run
  the suite at all, where today the database-marked tests would skip. Deliberately not solved with a
  container-started database: the development container has no Docker socket, so that fallback would
  work on the host and fail in the container (design D6).
