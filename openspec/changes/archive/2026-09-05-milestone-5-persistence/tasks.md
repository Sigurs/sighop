## 1. Dependencies and configuration (`database`)

- [x] 1.1 Add `sqlalchemy[asyncio]>=2.0`, `asyncpg>=0.30` and `alembic>=1.13` with `uv add`, and verify `uv lock --check` passes and `uv run python -c "import sqlalchemy, asyncpg, alembic"` succeeds
- [x] 1.2 Create `src/sighop/config.py` reading `DATABASE_URL` and `SIGHOP_SECRET_KEY` from the environment with no dotenv dependency (design D9); verify a unit test constructs the config from an explicit mapping rather than the real environment
- [x] 1.3 Validate the URL at construction: require the `postgresql+asyncpg` driver and reject libpq-only query parameters (`sslmode`, `options`) with a message naming what is unsupported (design D1); verify by tests for an accepted URL, a `+psycopg` URL and a `?sslmode=require` URL
- [x] 1.4 Make every rendering of the config redact the password — `repr`, log events and error messages; verify a test asserts the password string appears in none of them
- [x] 1.5 Add pool settings with defaults `pool_size=5`, `max_overflow=5`, `pool_timeout=10`, `pool_pre_ping=True` (design D6), each overridable; verify a test asserts the defaults and that the total is well under the measured role limit of 30
- [x] 1.6 Add explicit `connect_timeout=5` and `statement_timeout=5` defaults, overridable, so no operation inherits asyncpg's 60 s connect default against a host that blackholes rather than refuses (design D16); verify a test asserts both are passed to the driver and neither is left at the driver default
- [x] 1.7 Rewrite `.env.dev`'s `DATABASE_URL` to `postgresql+asyncpg://` and add a committed `.env.example` documenting both variables and the command that generates the secret; verify `.env.dev` is still gitignored and `.env.example` contains no credentials

## 2. Schema and migrations (`database`)

- [x] 2.1 Add `alembic/` with an async `env.py` driving the migration through `connection.run_sync` (design D1) and taking its URL from `config.py` rather than from `alembic.ini`; verify `alembic current` runs against the dev database and reports an empty revision
- [x] 2.2 Write the initial migration creating `entity`, `contact`, `path` and `packet_log` per design D3, with every timestamp `TIMESTAMPTZ` (design D7) and `node_hash` indexed but **not** unique on either table; verify `sighop db upgrade` against the dev database then `\d` shows the four tables
- [x] 2.3 Comment the four §6 tables the migration deliberately omits, naming the milestone that owns each, so absence reads as intent; verify by review of the migration file
- [x] 2.4 Implement `downgrade()` dropping the four tables; verify an upgrade→downgrade→upgrade cycle against a throwaway schema leaves the schema at head with no leftover objects
- [x] 2.5 Assert no application code calls `create_all()`: schema comes only from migrations (design D5); verify a test greps the package for it, in the same spirit as the existing single-`Data`-send assertion

## 3. Engine, session and failure containment (`database`)

- [x] 3.1 Create `src/sighop/db/engine.py` owning engine creation, session factory and disposal; verify a test opens and disposes an engine against the dev database and asserts no connection is left open
- [x] 3.2 Reject naive datetimes at the storage boundary and normalise everything to timezone-aware UTC (design D7); verify a round-trip test against the dev server — whose `TimeZone` is `Europe/Helsinki` — returns the same instant, and a naive value raises
- [x] 3.3 Distinguish the failure modes in connection errors: unreachable host, rejected credentials, role connection limit reached (`FATAL: too many connections for role`, observed during design) and schema-version mismatch; verify unit tests over the driver's error classes assert each maps to its own reported cause
- [x] 3.4 Add the schema-version check comparing the database's applied revision with the revision the code expects, failing startup with both versions and the command that reconciles them (design D5); verify tests for behind, ahead and matching
- [x] 3.5 Wrap repository operations so a post-startup failure returns an outcome rather than raising into the pipeline, increments a counter, emits an `error` wide event naming the operation, and sets a `degraded` flag that clears on the next success (design D8); verify a test with an engine that fails on demand asserts the flag sets and clears
- [x] 3.6 Add a bounded write-behind worker — bounded queue, drop-oldest, discard counter, batched flush — reused by paths and the packet log (design D2); verify tests for backlog under a slow sink, discard counting when full, and flush-on-shutdown
- [x] 3.7 Enforce the connect and statement bounds on every operation, mapping each expiry to a reported database error that sets `degraded` rather than a wait that continues (design D16); verify a test against a sink that never answers asserts the call returns within the bound and the flag is set
- [x] 3.8 Add the degraded-state probe: while degraded, attempt a lightweight connectivity check at a bounded interval with capped backoff (default 30 s), clearing the flag and firing a recovery hook on success (design D15); verify a test asserts the flag clears with no write ever attempted, and that the interval backs off while the database stays down
- [x] 3.9 Verify the reception path is unaffected by an unresponsive database: a test replays a capture with an engine that never answers and asserts per-packet decode and dispatch timings match a run with no database configured

## 4. Entity store and key sealing (`entity-store`, `local-identity`)

- [x] 4.1 Implement seed sealing with `nacl.secret.SecretBox` under a 32-byte key, stored as a version byte followed by the sealed box (design D4); verify round-trip, wrong-key failure and a tamper test that flips one ciphertext byte and asserts an authentication error
- [x] 4.2 Parse `SIGHOP_SECRET_KEY` as base64 of exactly 32 bytes, failing with a message that says whether it was missing, the wrong length or the wrong encoding — never padding, truncating or hashing it into shape; verify a test per case
- [x] 4.3 Add `sighop keys secret` generating a secret from the system CSPRNG and printing it once with the statement that losing it makes every stored identity unrecoverable; verify a test asserts the output decodes to 32 bytes and carries the warning
- [x] 4.4 Implement the entity repository: store and load id, type, name, public key, node hash, sealed seed, advert config and enabled state; verify a store→load round trip against the dev database reproduces the same public key and node hash
- [x] 4.5 On load, compare the public key derived from the decrypted seed against the stored `public_key` column and fail naming the entity on mismatch; verify a test corrupts the column and asserts the error names the entity and produces no key
- [x] 4.6 Extend the node-hash collision rule across persisted and keyfile-loaded entities, failing startup naming both sources and the shared hash, and feeding generation the taken hashes (design D14, §3 rule 3); verify tests for two colliding stored entities, and a keyfile colliding with a stored entity
- [x] 4.7 Add `sighop keys import <keyfile>` (fails naming the existing entity when the public key is already stored) and `sighop keys export <ref> <path>` (owner-only permissions, refuses an existing path, states the file holds private key material); verify tests for both paths of each
- [x] 4.8 Add `sighop keys list` printing name, type, public key, node hash and enabled state; verify a test asserts neither the seed nor its ciphertext appears in the output
- [x] 4.9 Keep key material out of every log event, error message and status line; verify a test drives an entity load failure and asserts the emitted event contains neither the seed nor the secret
- [x] 4.10 State at keyfile creation and export that the file holds an unencrypted seed protected only by its permissions, since the stored form is encrypted and the two must not be confused; verify by string comparison on both commands' output

## 5. Durable contacts (`contacts`)

- [x] 5.1 Add a contact repository with upsert on the public key and a load-all for startup, carrying `advert_verified` (design D12); verify an upsert→load round trip against the dev database preserves name, flags, both heard timestamps and the verified marker
- [x] 5.2 Give `ContactStore` an optional repository and write on observed change — created, updated or renamed, per the existing `ContactObservation` — never on every reception (design D2); verify a test asserts repeated identical adverts produce writes proportional to changes, not to receptions
- [x] 5.3 Restore contacts at startup before any traffic is processed, and keep `net/contacts.py` free of any SQLAlchemy import (design D10); verify a test resolves a restored peer by name and by key prefix before feeding any reception
- [x] 5.4 Keep a contact usable in memory when its write fails, reporting the failure and neither dropping the contact nor refusing the advert; verify a test with a failing repository asserts the contact remains addressable for the rest of the run
- [x] 5.5 Preserve the manual-addition distinction across the round trip, and let a later verified advert upgrade a stored manual contact to advert-verified with its name and flags; verify tests for both directions
- [x] 5.6 Confirm the unverified-advert rule is unchanged under persistence: a bad signature writes nothing; verify a test asserts zero rows after a badly-signed advert
- [x] 5.7 Verify durability across an ungraceful stop: a test kills the write path without a graceful shutdown and asserts a contact whose write had completed is present on reload
- [x] 5.8 Move contact writes off the advert subscriber onto a dedicated single-writer queue that **never drops** — on overflow the unpersisted marker carries the contact instead (design D16); verify a test asserts the subscriber returns without waiting for the write, and that an overflowed queue still leaves every contact marked unpersisted
- [x] 5.9 Track unpersisted contacts with an in-memory marker set when a write fails or is not attempted and cleared when one succeeds (design D15); verify tests assert the marker sets on failure, clears on success, and that a redundant clear-then-write is harmless because the upsert is idempotent
- [x] 5.10 Flush every marked contact on the recovery hook from 3.8, one upsert per public key carrying latest state (design D15); verify a test observes several adverts for one peer during a simulated outage, restores the database, and asserts exactly one row written with the latest state and no restart required
- [x] 5.11 Confirm the backfill closes the recovery window and not the crash window: a test restarts while the database is still unreachable and asserts the unwritten contacts are absent and the startup restored-count reflects what was persisted rather than what was observed
- [x] 5.12 Confirm the backfill is not extended to paths or the packet log (design D15); verify a test asserts a route and a packet-log row discarded during an outage are not rewritten on recovery

## 6. Durable paths (`path-learning`)

- [x] 6.1 Add a path repository upserting on (destination, path bytes, hash size) so re-hearing a route updates `confirmed_at` and `snr_db` rather than adding rows (design D12); verify a test hears one route twice and asserts one row with the later timestamp
- [x] 6.2 Persist the public-key and node-hash keyings distinctly, and keep an ambiguous entry ambiguous across the round trip; verify a test restores a hash-keyed entry and asserts it is still marked ambiguous
- [x] 6.3 Store an empty path as a valid zero-hop route, distinct from the absence of a row; verify a test restores a zero-hop route and asserts a lookup returns it while an unknown destination still reports no path
- [x] 6.4 Write routes through the bounded write-behind worker so the reception path never waits, counting discards (design D2); verify a test learns routes faster than the sink accepts them and asserts reception throughput is unaffected and discards are counted
- [x] 6.5 Restore routes at startup with their original `confirmed_at`, so a live confirmation beats a restored route under the existing most-recently-confirmed rule (spec: restoration is not confirmation); verify a test restores one route, learns another, and asserts the fresh one wins with both retained
- [x] 6.6 Answer every lookup from memory including while the database is unreachable, and keep `net/paths.py` free of any SQLAlchemy import; verify a test performs lookups with a failing repository and asserts no error and no slowdown

## 7. Packet log (`packet-log`)

- [x] 7.1 Add a packet-log repository writing RX rows (packet id, direction, time, route and payload types, path, hop count, size, SNR, RSSI, airtime, outcome) and TX rows (entity, priority class, result), batched through the write-behind worker; verify a test writes a batch of each and reads them back
- [x] 7.2 Record an undecodable frame with its raw bytes and the reason, so §4.1's rule reaches the database; verify a test feeds a malformed frame and asserts a row with `raw` set and a reason
- [x] 7.3 Guarantee logging never delays or fails a packet: full buffer discards oldest and counts, database failure degrades rather than blocks; verify a test drives a slow sink and asserts reception rate is unchanged and the discard counter moves
- [x] 7.4 Add the pruning task enforcing a configurable row cap defaulting to 100 000 (design D3, open question 2), running periodically, reporting rows deleted, pruning once at startup when already over the cap, and surviving a failed pass; verify tests for over-cap pruning at startup, periodic pruning, and a failing pass followed by a successful one
- [x] 7.5 Assert the log is a feed, not a dependency: nothing consults it for dedup or protocol behaviour; verify a test runs a full replay with the packet log disabled and asserts identical decode, dedup and scheduler outcomes

## 8. Runtime and command line (`runtime-cli`)

- [x] 8.1 Add `--database-url` overriding the environment, and run with neither supplied; verify tests for environment-only, override, and neither
- [x] 8.2 Open the database at startup, run the version check, load entities, contacts and paths, and start the pruner and write-behind workers; verify a replay run against the dev database reaches steady state and stops cleanly
- [x] 8.3 Report at startup whether the run is persistent or in-memory, and when persistent the database in force, the applied schema version and the counts of entities, contacts and paths restored; verify render tests by string comparison for both modes
- [x] 8.4 Load entities from the store as well as from `--entity` keyfiles, reporting each with its source, and apply the collision rule across both (design D14); verify tests for store-only, keyfile-only, and both together, plus the colliding case
- [x] 8.5 Make a replay run non-persisting by default with an explicit opt-in flag, stating which at startup (design D13); verify a test replays a capture against a configured database and asserts zero rows written, and that the opt-in writes
- [x] 8.6 Extend the periodic status line with persistence state (off / on / degraded), packet-log rows discarded, routes discarded and contacts awaiting backfill; verify render tests assert the three states are distinguishable, that the counters read zero rather than being omitted, and that `degraded` clears without any write once the probe succeeds
- [x] 8.7 Add `sighop db upgrade` and `sighop db current` printing applied and expected revisions and whether they agree; verify both against the dev database
- [x] 8.8 Fail `sighop run` against an unmigrated or unknown-revision database, naming both revisions and the reconciling command, and applying nothing; verify a test against a schema deliberately left one revision behind
- [x] 8.9 Make entity-store commands fail clearly when no database is configured while keyfile commands stay usable; verify a test per command
- [x] 8.10 Confirm the whole existing surface is unchanged with no database configured — monitor, replay, capture, `keys new`/`show`, the transmit gate default; verify the existing suite passes with `DATABASE_URL` unset

## 9. Test harness against the development database

- [x] 9.1 Add a session-scoped fixture creating `sighop_test_<random>`, setting the search path, running the real migration chain into it and dropping it with `CASCADE`, because the role cannot `CREATEDB` (design D11); verify a database test runs and leaves no schema behind
- [x] 9.2 Drop stale `sighop_test_` schemas at session start so a crashed run does not accumulate them; verify by creating one by hand and asserting the next session removes it
- [x] 9.3 Gate every database test behind a marker that skips when no test database is configured; verify the suite passes with the variable unset and reports the database tests as skipped
- [x] 9.4 Keep the suite's connection use inside the pool bound so a parallel run cannot exhaust the role's 30-connection limit; verify a test asserts the configured ceiling and that the fixture disposes its engine
- [x] 9.5 Extend `tests/protocol/test_import_boundary.py` with an explicit assertion that `protocol/` imports nothing from `db/` (design D10); verify the test fails when the import is added deliberately

## 10. Verification and design record

- [x] 10.1 Run the full check — `uv run ruff check`, `uv run ruff format --check`, `uv run mypy src tests`, `uv run pytest` — with and without `DATABASE_URL` set, and verify both pass
  - `ruff check`: **passes**. `pytest`: **passes both ways** — 696 passed / 58 skipped with `DATABASE_URL` unset, 754 passed with it set. `mypy src`: **clean**.
  - `ruff format --check` and `mypy src tests` **do not pass, and did not pass at HEAD either**: 30 unformatted files and 63 type errors, all of them pre-existing and all in code this milestone did not write. Measured by stashing the change and re-running: 30 unformatted before / 30 after (**0 added**), 63 mypy errors before / 63 after (**0 added**), and none of either in the new modules or new tests.
  - Left as-is by an explicit decision: reformatting 30 untouched files and fixing 63 type errors across 12 older test files is unrelated to persistence and would dominate this change's diff. Tracked as follow-up work, not absorbed here.
- [x] 10.2 Confirm `tests/protocol/test_foreign_decrypt.py` and the 997-frame corpus tests are untouched and still pass, since milestone 4's exit criterion must keep proving itself (design D14)
- [x] 10.3 Execute the migration plan against the dev database: apply migrations, generate the secret, import the development keyfile, run receive-only long enough to accumulate contacts, paths and packet-log rows, restart, and verify the restored counts appear in the startup line
- [x] 10.4 Kill the process without a graceful stop and verify contacts survived while the discard counters report what did not, then record the measured write rates and row counts
- [x] 10.5 Exercise the outage path against the dev database: with the runtime receiving, make Postgres unreachable for several minutes and verify reception and scheduling are unaffected and the status line reads `degraded`; then restore it and verify the probe clears the flag and backfills the contacts observed meanwhile, with no restart (design D15, D16). Record how long detection actually took
- [x] 10.6 Re-derive the `packet_log` retention default and the path candidate growth from that run (design open questions 1 and 2), and record whether the defaults stand or change
- [x] 10.7 Update DESIGN.md in this change per its own rule: §2's DB-access row (driver confirmed against a live database), §6 (the four tables that exist and the four that do not yet, and how keys are sealed), §11 (`db/`, `alembic/`, `config.py`), and §12's milestone 5 entry recording what was found
- [x] 10.8 Record any observation bearing on §13 unknown #4 — whether a peer that learns us only from a zero-hop advert floods its replies — if the opportunity arises during 10.3, and leave the unknown open rather than closing it on inference
