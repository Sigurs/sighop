## Why

Everything sighop knows today dies with the process. Milestone 4 recorded the consequence
plainly: *"the run that sends must hear the peer's advert in that same run; a restart forgets
every contact"* — which is what `--peer-wait` exists to paper over, and what DESIGN.md §12 names
milestone 5 as the fix for. Contacts, learned paths and the packet feed are all in-memory, and
the one thing that *is* on disk — an entity's private seed — sits in a plaintext keyfile, which
§6 says is exactly what must not happen to the platform's crown jewels.

Milestone 6 cannot start without this. A room server whose whole value proposition is *"message
history is durable and survives reboot, per spec — the whole reason a room server beats a
walkie-talkie"* (§6) needs a database underneath it, and its ACLs must survive restart *"or every
member re-authenticates"* (§7). Milestone 5 is the floor those are built on, and it is the first
milestone whose risk is ordinary — no radio, no regulator, no one-way door.

## What Changes

- **A database layer.** SQLAlchemy 2.0 async over asyncpg, with Alembic as the only authority on
  schema. `src/sighop/db/` gains models and repositories; `alembic/` gains an async `env.py` and
  the initial migration. The dev database is real and already probed: **PostgreSQL 18.4**, role
  `apps_sighop-dev`, non-superuser, `rolcreatedb` false, `rolconnlimit` 30, empty, `public`
  writable, `plpgsql` the only extension. Everything below is sized to that.
- **Four tables, not eight.** §6 sketches eight; this milestone builds the four the current
  runtime actually reads and writes — `entity`, `contact`, `path`, `packet_log` — so every table
  ships with behaviour and a test behind it. `room`, `room_member` and `message` belong to
  milestone 6 and `bot_state` to milestone 7, each in its own migration. §6 calls its list a
  sketch and *"not final DDL"*; committing four tables nothing reads would make it final by
  accident.
- **Private keys are encrypted at rest.** An entity's seed is sealed under a 32-byte
  `SIGHOP_SECRET_KEY` from the environment and never stored in the clear, so *"a DB dump must not
  be sufficient to impersonate a room server"* (§6) is a property the schema enforces rather than
  a hope. A missing or malformed secret is a startup failure, never a silent fallback to plaintext.
- **Keyfiles become an import/export format.** Milestone 4's keyfile was declared *"explicitly a
  bridge… milestone 5 imports it and encrypts it at rest"* (design D1). It stops being the
  runtime's identity source and becomes what §6 asks for: `sighop keys export` / `import`, so
  *"operators can back identities up deliberately"*. The burned test fixture keeps working —
  `tests/protocol/test_foreign_decrypt.py` must decrypt on every commit exactly as it does now.
- **Contacts and paths survive the process.** Both stores keep their present interfaces and gain a
  durable implementation behind them. Contacts are written as adverts verify, never dropped, and
  written again when the database returns from an outage — because re-acquiring one means waiting
  for the peer to advert, and the advert floor is 24 h. Paths are written behind the RX path and
  dropped freely when they cannot be, because a route is relearned from the next reception. Neither
  write ever delays the decode stage.
- **A bounded packet log.** `packet_log` becomes the ring buffer §6 describes — *"aggressively
  pruned… unbounded packet logging on a busy mesh will fill a disk"* — written best-effort off the
  RX path, pruned by a periodic task, and reported in the status line. This is the table that
  makes milestone 8's live feed possible without inventing storage then.
- **The database is optional.** Without a configured URL, `sighop run` behaves exactly as it does
  today with in-memory stores, and `sighop monitor`, replay and the entire existing test suite
  need no Postgres. Persistence is a repository swapped in behind an interface, not a new
  precondition for running the radio.
- **Configuration gets a home.** `src/sighop/config.py` — listed in §11 and never yet written —
  reads `DATABASE_URL` and `SIGHOP_SECRET_KEY` from the environment. No dotenv dependency:
  `uv run --env-file .env.dev` already does it.
- **A correction to `.env.dev` rather than to the design.** Its URL reads `postgresql+psycopg`;
  §2 says asyncpg. The URL is rewritten to `postgresql+asyncpg`, so Alembic runs its migrations
  through an async `env.py` and the codebase carries one driver.

## Capabilities

### New Capabilities
- `database`: connecting to Postgres and owning the session lifecycle — pooling sized to the
  role's connection limit, `TIMESTAMPTZ`-only timestamps against a server whose `TimeZone` is not
  UTC, Alembic as the sole schema authority with a startup check that refuses a database that is
  not at head, the rule that a configured-but-unreachable database is a startup failure while an
  unconfigured one is not, and transaction boundaries that keep a database fault from stalling the
  radio.
- `entity-store`: the `entity` table — identity rows whose private seed is sealed under
  `SIGHOP_SECRET_KEY` and never written in the clear, key import and deliberate export, the
  refusal to start on a missing or malformed secret, and node-hash collision rejection extended
  across persisted entities as well as loaded ones.
- `packet-log`: the bounded RX/TX ring buffer — best-effort writes that never block reception,
  a row cap with pruning, an explicit count of what was dropped when the writer falls behind, and
  its standing as a feed rather than an audit trail.

### Modified Capabilities
- `contacts`: the requirement *"The contact store is in-memory for this milestone"* is replaced.
  Contacts are loaded at startup and written through as verified adverts arrive, so a peer heard
  yesterday is addressable today; the in-memory store remains the behaviour when no database is
  configured, and the startup output says which of the two is in force.
- `path-learning`: the requirement *"The path store is shared and in-memory"* is amended. The
  store stays shared, single and authoritative in memory, and gains a durable backing written
  behind the RX path; on startup, learned routes are restored rather than relearned from traffic.
  Eviction and most-recently-confirmed-wins are unchanged.
- `local-identity`: keyfiles stop being the runtime's identity source and become an interchange
  format. The seed at rest moves from a `0600` JSON file to an encrypted column; import, export
  and the burned-fixture rule are restated against that.
- `runtime-cli`: `sighop run` gains database configuration and reports at startup whether it is
  persistent or in-memory, which entities came from the database, and how many contacts and paths
  were restored. A new `sighop db` command surface applies and reports migrations.

## Impact

- **New dependencies:** `sqlalchemy[asyncio]>=2.0`, `asyncpg>=0.30`, `alembic>=1.13`. All three
  are named or implied by §2; nothing else is added, and the cryptography for key sealing comes
  from `pynacl`, already a dependency.
- **New code:** `src/sighop/db/` (models, repositories, engine/session), `src/sighop/config.py`,
  `alembic/` with an async `env.py` and one migration, and `.env.example` committed alongside the
  gitignored `.env.dev` so the required variables are documented without the secrets.
- **Modified code:** `src/sighop/net/contacts.py` and `src/sighop/net/paths.py` (a repository
  behind the existing interface, not a rewrite), `src/sighop/keystore.py` (import/export),
  `src/sighop/runtime.py` (open the database, restore state, start the pruner), `src/sighop/cli.py`
  (`sighop db`, `--database-url`, `keys import`/`export`), `src/sighop/monitor/render.py` (startup
  lines and the status line's persistence fields).
- **`protocol/` gains nothing and imports nothing new.** *"`protocol/` must have no dependency on
  `db/` or `net/`"* (§11) is enforced by `tests/protocol/test_import_boundary.py`, which must keep
  passing unchanged. `db/` is a peer of `net/`, not a layer beneath `protocol/`.
- **No test may require Postgres to pass.** The existing suite runs with no database. Database
  tests are additive and skip when `DATABASE_URL` is unset — and because the role cannot
  `CREATEDB`, they isolate in a throwaway **schema** inside the dev database rather than a
  throwaway database.
- **No change to the radio, the scheduler, the budget or the gate.** This milestone transmits
  nothing new and relaxes no safety property. `--enable-transmit` is untouched and still off by
  default.
- **DESIGN.md is updated in this change**, per its own rule: §2's DB-access row records that the
  driver decision was re-confirmed against a live database rather than assumed, §6's sketch
  becomes the four tables that exist plus the four that do not yet, §11 gains `db/`, `alembic/`
  and `config.py`, and §12's milestone 5 entry records what was found.
- **Out of scope, deliberately:** room login, ACLs, history storage and sync, and retention
  policy (milestone 6, which owns `room`, `room_member` and `message`); `bot_state` and bots
  (milestone 7); the WebUI and the live feed that reads `packet_log` (milestone 8); the container,
  compose file and secret delivery (milestone 9); path scoring beyond
  most-recently-confirmed-wins (§13 unknown #3, still awaiting two routes to one peer); and any
  transmission aimed at the public mesh.
