# Design

## Context

See proposal.md — Why. The shape that matters for the approach:

- `cli.py` is 4288 lines and mixes three unrelated things: an argparse tree, a set of admin command
  implementations that the panel now duplicates, and the boot logic that assembles the node
  (`open_persistence`, `migrate_on_start`, `_run_config`, `_attach_web`, `_run_live`, `_run_replay`,
  `_live_startup`, `_database_config`). Only the third is load-bearing.
- `Config.from_environment` already exists and already reads `DATABASE_URL`, `SIGHOP_SECRET_KEY`,
  `SIGHOP_DB_SCHEMA` and `SIGHOP_PATH_HASH_SIZE`. It takes `database_url` as a keyword argument
  purely so the command line can override the environment. The frozen `Config` dataclass is the
  natural home for the ~22 settings the `run` flags carry today.
- `Runtime` takes an event source, a startup callable, a config, a sender and an optional
  `persistence`. It is already independent of argparse; `_run_live` is a 30-line function that
  constructs it. Nothing about removing the command line touches the runtime's own structure.
- `persistence: Persistence | None` threads through `Runtime` with roughly 14 `if self.persistence is
  None` branches, and through `web/render.py` and the panel's pages as a third state beside "present"
  and "degraded".
- `tests/dbfixtures.py` already creates a throwaway schema, applies the real migration chain into it
  and drops it with `CASCADE`. Only where the *server* comes from changes.
- `build.sh` has two gates that invoke the command line: `smoke` runs `--help` and `run --help`,
  `replay` runs `sighop run --replay` on the host and in the image and diffs the output.

## Goals / Non-Goals

**Goals:**

- One boot path, configured from one place, with no second code path for a node that stores nothing
  or serves no interface.
- The deletion is a deletion: `cli.py` goes away rather than shrinking into a smaller parser.
- The musl-versus-glibc parity check survives the command line it is currently written against.
- The test suite gets stronger, not weaker: every persistence test runs on every run.

**Non-Goals:**

- Re-litigating what the panel can do. Where the panel already has a route, this change deletes the
  command and stops; it adds no panel features except where a spec delta says otherwise (it does not).
- Account management in the panel. Deliberately deferred — see proposal.md, Risks.
- Changing the runtime's own architecture, the wire protocol, the database schema or the panel's
  layout. No migration is written by this change.
- Making the node reconfigurable without a restart. Environment variables are read once, at start.

## Decisions

### D1 — `src/sighop/boot.py` is the new entry point; `cli.py` is deleted, not refactored

The boot functions move to a new `boot.py` with a `main() -> int` that takes no arguments, and
`[project.scripts]` points `sighop` at it. Everything else in `cli.py` is deleted outright.

*Why not shrink `cli.py` in place?* Because the file's name and structure are the parser, and a
4288-line file edited down to 300 keeps every import, every helper and every habit of the command
surface alive in review. A new file makes the diff state plainly what survived.

*Alternative considered:* keeping `main(argv)` with an empty argv for test convenience. Rejected —
the argv parameter is what tests would keep reaching for, and it would quietly reintroduce an
argument surface. Tests construct `Runtime` directly, which is what the runtime tests already do
underneath `cli.main`.

### D2 — Every setting becomes a field on `Config`, read by `Config.from_environment`

The ~22 `run` flags become `SIGHOP_*` variables parsed into the existing frozen `Config` dataclass,
validated at parse time, with each parse error naming its variable and value. `Config.database`
stops being `| None`. `from_environment` loses its `database_url` keyword.

Naming follows the flags: `--device` → `SIGHOP_MODEM` (the name compose already uses),
`--enable-transmit` → `SIGHOP_ENABLE_TRANSMIT`, `--duty-cycle-ceiling` →
`SIGHOP_DUTY_CYCLE_CEILING`, `--status-interval` → `SIGHOP_STATUS_INTERVAL`, `--radio-preset` →
`SIGHOP_RADIO_PRESET`, `--web-host`/`--web-port`/`--web-allowed-host` → `SIGHOP_WEB_HOST`,
`SIGHOP_WEB_PORT`, `SIGHOP_WEB_ALLOWED_HOSTS` (comma-separated, because an environment variable
cannot repeat), `--dedup-ttl`/`--dedup-max-entries` → `SIGHOP_DEDUP_TTL` /
`SIGHOP_DEDUP_MAX_ENTRIES`, `--capture` → `SIGHOP_CAPTURE_FILE`, `--log-file` → `SIGHOP_LOG_FILE`.

Dropped rather than translated, because they exist only to drive an exercise from a terminal:
`--stub`, `--entity`, `--peer`, `--send`, `--allow-flood`, `--advert-zero-hop`, `--peer-wait`,
`--advert-override-seconds`, `--advert-override-expires-in`, `--replay`, `--persist-replay`,
`--migrate` (now unconditional) and `--database-url` (the environment is the only source).
`--entity` in particular is dropped because a required database makes the entity store the only
place an identity can live; identities held as keyfiles must be imported through the panel first.

*Why one flat `Config` rather than a config file?* A file is a second configuration mechanism to
document, validate and keep in sync with compose, and the deployment already delivers environment
variables. `SIGHOP_WEB_ALLOWED_HOSTS` as a comma-separated list is the one place the environment is
a worse fit than a flag; a separator is cheaper than a file format.

### D3 — Refusals happen in one ordered startup check, before anything is opened

A single `check_environment(config) -> list[str]` runs before the modem is opened, the database is
connected or the interface is bound, and returns every problem it found rather than the first. Order:
database URL present and parseable, secret key present and the right shape, then every other setting.
The secret-key refusal prints the generating command:

```
SIGHOP_SECRET_KEY is not set.
Generate one with:

    openssl rand -base64 32
```

*Why all problems rather than the first?* An operator filling in a new `./.env` otherwise restarts
the container once per missing variable. *Why not generate a secret when it is missing?* A secret the
operator has not recorded seals identities that cannot be unsealed after the next restart — the
failure surfaces days later and is unrecoverable. Printing the command keeps the recording step with
the person who must keep it.

### D4 — `persistence` stops being optional at the type level

`Runtime.persistence` becomes `Persistence` rather than `Persistence | None`, and every `if
self.persistence is None` branch is deleted rather than made unreachable. Same for
`web/render.py`'s three-state rendering, which becomes two: present, and degraded. `PERSISTENCE_OFF`,
`WEBHOOKS_OFF_NO_DATABASE`, `NO_DATABASE`, `BOTS_OFF`, `ROOMS_OFF` and `CHANNELS_OFF` are deleted.

*Why the type change and not just the branches?* Deleting branches while the annotation still admits
`None` leaves the next change free to reintroduce them. mypy already runs over `src`; the annotation
is what makes the deletion stick.

`WEBHOOKS_OFF_REPLAY` stays: the replay harness still must not fire webhooks.

### D5 — Replay becomes `python -m sighop.replay`, a module with one positional argument

`src/sighop/replay.py` gains a `__main__` block that takes exactly one capture path, opens no
database and no modem, and drives `CaptureReplay` through the same decode path. It is not a command
line: one positional argument, no flags, no subcommands, and it is not on `PATH`.

*Why keep it at all?* `build.sh`'s parity gate is the only place the platform's behaviour is checked
on musl, because the suite runs on the build host's glibc. Deleting it would remove a real check to
save a 40-line module. *Why not an environment variable on the node (`SIGHOP_REPLAY`)?* Because that
would make replay a mode of the production entry point again, and the node now requires a database
and a web interface that a parity check has no use for. A separate module keeps the gate's
dependencies to none.

The `--status-interval 3600` the gate passes today exists to keep a status line out of the diff; the
module simply never prints one.

### D6 — The suite requires a configured database and works in a schema of its own

The environment must name a database — `SIGHOP_TEST_DATABASE_URL` or `DATABASE_URL` — and the run
creates a `sighop_test_<random>` schema inside it, applies the real migration chain there, and drops
it with `CASCADE` when the run ends. That machinery already exists and is unchanged; what changes is
that it is no longer optional.

`pytest.ini`'s `database` marker, `conftest.pytest_collection_modifyitems`, `SKIP_REASON` and all 516
`@pytest.mark.database` decorators are deleted. A run with no database configured stops in
`pytest_configure`, before collection, with one message naming both variables — `pytest.exit` with
`USAGE_ERROR`, not a skip, because a suite that could not reach a database has not passed.

*Why not a `testcontainers` PostgreSQL when nothing is configured?* It was the first approach and was
abandoned: the development container carries no Docker socket, so the fallback would work on the host
and fail in the container — the opposite of what a test harness should do, and it would make the
suite unrunnable exactly where most of the work happens. Requiring a named database is the one rule
that holds in both places.

*Why not run migrations once and share the schema across runs?* The per-run schema is what lets two
developers, or a developer and CI, point at one database without taking each other's tables out, and
it is what makes the migration chain itself part of what every run exercises.

*Leftovers*: a killed run cannot drop its schema, so the sweep at session start removes any
`sighop_test_` schema it finds before creating this run's own. That is why they share a prefix.

### D7 — The smoke gate proves imports and the refusal path; the parity gate drives the module

`smoke` becomes two runs under the same constrained `docker run`: `python -c "import sighop.web.app,
sighop.runtime"` for the import property the `--help` run was really testing, and the entry point
with an empty environment, asserting a non-zero exit and the `openssl` line in its output. `replay`
invokes `python -m sighop.replay <capture>` on the host and in the image and diffs as before, now
with no `DATABASE_URL` needed on either side.

*Why keep an import check when the refusal check also imports?* The refusal check exits before the
web application is constructed; the import check is what catches a template or static asset missing
from the image.

## Risks / Trade-offs

- **A locked-out operator has no recovery.** With account management gone and `/setup` closed once an
  account exists, a forgotten password means editing the database by hand. → Accepted deliberately
  (proposal.md, Risks). The follow-up change that adds panel account routes should be scheduled
  before this is deployed anywhere with more than one operator.
- **A stored channel pre-shared key cannot be read back.** Found during apply: `sighop channel
  key` was the only surface that printed one. → Accepted deliberately (operator decision); stated in
  `channel-store` and on the chat page, so an operator keeps the key they add.
- **`test_web_write_parity.py` loses its central assertion.** Its whole point is that a panel write
  *is* the repository call the command makes; with no command, the pairing is gone. → The panel half
  of every test is kept and rewritten to assert against the repository call directly. This is 2828
  lines of the most load-bearing tests in the project and is the largest single risk in the task
  list; it is split into its own group and done before the command implementations are deleted, so
  the old assertions are still there to compare against.
- **Docker becomes a hard prerequisite for the suite.** A developer without a container engine and
  without a database cannot run any test, where today most would pass. → Stated in the `database`
  spec delta as an explicit failure with a message naming both options, rather than a confusing
  connection error.
- **Every capture is now historical.** Nothing records new ones. → The runtime's own capture writing
  is kept behind `SIGHOP_CAPTURE_FILE`, so a future change can expose it without rebuilding it.
- **A settings typo now fails at container start rather than at a prompt.** An operator gets a
  restart loop instead of a shell error. → D3's all-problems-at-once check plus compose's
  `restart: unless-stopped` behaviour means the first log line names everything to fix.
- **`monitor`'s live terminal view has no exact replacement.** The panel's feed covers it for an
  operator with a browser; someone on a serial console has nothing. → Accepted; the panel is the
  supported surface and the node's own rendered output still names every reception.

## Migration Plan

No data migration: the schema is untouched. The operator-facing migration is one edit to each host's
`./.env` and a `docker compose up -d`:

1. Before upgrading, confirm the panel has an account (`/setup` has been completed) and that any
   identity held only as a keyfile has been imported — after the upgrade there is no `keys import`.
2. Move every setting that `compose.yaml` passed as an argument into the host's `./.env` as its
   `SIGHOP_*` variable. The updated `.env.example` lists each one against the flag it replaces.
3. `docker compose up -d`. The node applies outstanding migrations by starting, as it already did
   with `--migrate`.

**Rollback**: the previous image, unchanged. Nothing in this change writes a schema revision, so an
older image starts against the same database without a downgrade. The `command:` block must be
restored in `compose.yaml` for the older image to be configured — keep the previous `compose.yaml`
alongside the previous image tag.
