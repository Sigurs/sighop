## Context

See proposal.md — Why. What follows is the state the design has to fit into, and it is unusually
well determined for a milestone that has not started: the target database was probed before this
document was written.

**The development database, measured rather than assumed** (`172.20.4.20:30432`, credentials in
the gitignored `.env.dev`):

| Property | Value | Consequence |
|---|---|---|
| Server | PostgreSQL 18.4 (Debian, aarch64) | Everything modern is available; nothing exotic is needed |
| Role | `apps_sighop-dev`, not superuser | No `CREATE EXTENSION` beyond trusted ones; none are wanted |
| `rolcreatedb` | **false** | **Tests cannot create a throwaway database** (D11) |
| `rolconnlimit` | **30** (server `max_connections` 100) | Pool sizing is a real constraint, not a formality (D6) |
| Schema | `public`, `CREATE` granted | The application owns `public` in its own database |
| Extensions | `plpgsql` only | No `pgcrypto`; encryption happens in Python anyway (D4) |
| `TimeZone` | **`Europe/Helsinki`** | Not UTC. `TIMESTAMPTZ` is not optional (D7) |
| Tables | none | Greenfield; the first migration is genuinely first |

Two of those were discovered, not expected, and both change the plan: no `CREATEDB` decides the
test isolation strategy, and a non-UTC server time zone turns a stylistic preference about
timestamp types into a correctness rule.

**The code this lands in.** Four milestones have built a pipeline with no database in it at all.
`net/contacts.py` holds a `ContactStore` keyed by public key with a node-hash index;
`net/paths.py` holds a `PathStore` of `LearnedPath` candidates in an `OrderedDict` with
least-recently-updated eviction; `keystore.py` reads and writes `0600` JSON keyfiles;
`runtime.py` composes the lot. The layering rule in §11 — `protocol/` depends on neither `db/`
nor `net/` — is enforced by `tests/protocol/test_import_boundary.py` and must survive untouched.

**What must not regress.** The 997-frame corpus replays through the decode path and reproduces
every reception exactly; `tests/protocol/test_foreign_decrypt.py` decrypts a stock-firmware DM on
every commit using a committed burned keyfile. Neither may acquire a database dependency.

## Goals / Non-Goals

**Goals:**

- A schema owned by migrations, applied to a real Postgres, with a startup check that the running
  code and the database agree.
- Entity identities in the database with seeds encrypted at rest, and keyfiles demoted to an
  import/export format.
- Contacts and paths that survive a restart, behind the interfaces they already have, without the
  reception path ever waiting on a database.
- A bounded packet log that cannot fill a disk and cannot stall the radio.
- Persistence that is optional in the strong sense: the existing suite, replay and monitor need
  no Postgres, and the radio keeps running when the database does not.

**Non-Goals:**

- Any of the four §6 tables milestones 6–7 own. No speculative DDL.
- A repository abstraction general enough for a second backend. There is one database and it is
  Postgres; SQLite compatibility would cost `bytea`, `TIMESTAMPTZ` and upsert semantics for a
  portability nobody has asked for.
- Read models for the WebUI. Milestone 8 knows what it needs to query; guessing now produces
  indexes for queries that do not exist.
- Connection pooling infrastructure (pgbouncer) or read replicas. One process, 2–5 entities.
- Any change to the radio, the scheduler, the airtime budget, the advert floor or the transmit
  gate.

## Decisions

### D1 — asyncpg, with an async Alembic `env.py`, and `.env.dev` corrected to match

DESIGN.md §2 says asyncpg; `.env.dev` arrived saying `postgresql+psycopg`. The operator's call is
**asyncpg**, so the URL is rewritten rather than the design.

The cost is paid in exactly one place. asyncpg has no synchronous mode, so Alembic's migration
runner — which is synchronous by construction — cannot drive it directly. `env.py` therefore uses
the async engine and bridges through `connection.run_sync(do_run_migrations)`, which is the
documented pattern and about fifteen lines. That is the whole of it: no second driver, no second
URL, no split configuration.

Two asyncpg behaviours are worth naming now so they are not rediscovered as bugs:

- **libpq query parameters are not understood.** `?sslmode=`, `?options=` and friends are psycopg
  spellings; asyncpg takes `ssl` and server settings through connect arguments. The dev URL has
  none, so this bites the first time the container meets a TLS-terminating deployment (milestone
  9), and the config module rejects an unsupported query parameter with a message rather than
  passing it through to a confusing driver error.
- **asyncpg caches prepared statements per connection**, which is a known hazard behind a
  transaction-pooling proxy. There is no proxy here and none planned; recorded so the eventual
  pgbouncer conversation starts from the right place.

*Alternative rejected:* psycopg3, which would have served both the async application and a
synchronous Alembic from one driver and needed no bridge. It is the smaller-moving-parts choice
and it is what the supplied URL already said. Overruled deliberately: §2 named asyncpg, the
operator confirmed it, and the async `env.py` is a fifteen-line one-time cost rather than an
ongoing one.

### D2 — Memory stays the authority; the database is a durable backing, written asynchronously

The obvious shape — repositories that *are* the store, queried per lookup — is wrong here. Route
lookup happens inside packet composition, contact lookup inside the MAC trial for every candidate
of every inbound `TXT_MSG`, and both sit downstream of a decode path whose defining property is
that replaying a capture reproduces every reception exactly. A database round trip in any of them
buys nothing and risks the property.

So each store keeps its present in-memory structure and its present interface, gains a repository
it writes to, and is *loaded* from that repository once at startup. Lookups never touch the
database. The three stores differ only in how eagerly they write, and the difference is driven by
measured rates:

| Store | Write policy | Why |
|---|---|---|
| `contact` | **Written promptly, off the subscriber path, never dropped** | Adverts are rare — 92 in the 1003-record corpus — and a contact is expensive to re-acquire: you must wait for the peer to advert again. Milestone 4's `--peer-wait` exists precisely because of that wait. So it is written at once and retried until it lands (D15), but not inline (D16) |
| `path` | **Write-behind**, bounded queue, drop-oldest | Learned on a large fraction of receptions. A route is relearned from the next packet, so a dropped write costs nothing but a counter |
| `packet_log` | **Write-behind**, batched, drop-oldest | 555 receptions in 2 h 54 min measured live (≈191/hour). Highest volume, lowest value per row |

"Observed change" is doing work in the contact row: `last_heard` moves on every advert, and the
existing `ContactObservation` already distinguishes created / updated / renamed. The write follows
that signal rather than the reception, which keeps a peer adverting every 24 h from generating
writes proportional to how often we hear its floods.

*Alternative rejected:* database-as-store with a read-through cache. It is the conventional shape
and it inverts the failure mode: an unreachable database would then make route lookup fail rather
than merely stop being durable, and the radio would go down with Postgres. §4.3's whole posture is
that the radio path degrades gracefully; this keeps it.

### D3 — Four tables, and the initial migration says so out loud

```
entity      id           uuid pk
            type         text            -- room_server | companion | bot
            name         text not null
            public_key   bytea(32) unique not null
            node_hash    smallint not null           -- derived, indexed, NOT unique
            sealed_seed  bytea not null              -- D4
            advert_config jsonb not null
            enabled      boolean not null default true
            created_at   timestamptz not null

contact     public_key   bytea(32) pk
            node_hash    smallint not null  (index)
            name         text null
            node_type    smallint null
            flags        smallint null
            advert_verified boolean not null
            first_heard  timestamptz not null
            last_heard   timestamptz not null

path        id           bigserial pk
            dest_public_key bytea(32) null           -- exactly one of these two
            dest_node_hash  smallint null            -- is set; ambiguous when hash
            path_bytes   bytea not null              -- may legitimately be empty
            hash_size    smallint not null
            hop_count    smallint not null
            snr_db       real null
            confirmed_at timestamptz not null
            packet_id    text null
            unique (dest_public_key, dest_node_hash, path_bytes, hash_size)

packet_log  id           bigserial pk
            packet_id    text not null (index)
            direction    text not null               -- rx | tx
            at           timestamptz not null (index, for pruning)
            route_type / payload_type / path_bytes / hop_count / size_bytes
            snr_db / rssi_dbm / airtime_ms
            entity_id    uuid null                   -- tx only
            priority_class smallint null             -- tx only
            outcome      text not null
            raw          bytea null                  -- undecodable frames
```

Three details are deliberate. `node_hash` is **not** unique on either table — §3 says collisions
are routine and the whole design assumes candidate sets, so a unique constraint there would be a
bug waiting for a busy mesh. `path_bytes` empty is a **valid zero-hop route**, distinct from no
row at all, which is the same distinction `PathStore` already draws and the reason its lookup
reports "no path" rather than returning an empty one. And `packet_log.raw` exists so §4.1's rule —
"a malformed frame we silently drop is invisible forever" — reaches the database too.

The four §6 tables that are *not* here (`room`, `room_member`, `message`, `bot_state`) get a
comment in the initial migration naming the milestone that owns each, so the next person reads
absence as intent.

*Alternative rejected:* all eight now. §6 calls its list a sketch and "not final DDL"; milestone 6
will discover things about ACLs and retention that change those four tables, and shipping them
untested makes the first real migration a rewrite rather than an addition.

### D4 — Seeds are sealed with libsodium's secretbox under a 32-byte `SIGHOP_SECRET_KEY`

§6 requires encryption at rest with a key from the environment. The construction: **XSalsa20-Poly1305
secretbox** (PyNaCl's `SecretBox`), which is already a dependency because the identity code uses
it, and whose API generates the nonce internally — the one thing most likely to be got wrong by
hand. The stored value is a version byte followed by the sealed box, so a future re-key has
somewhere to declare itself.

The secret itself:

- **Exactly 32 bytes**, supplied base64-encoded in `SIGHOP_SECRET_KEY`. A value of the wrong
  length or encoding is a startup failure that says which; it is never padded, truncated or
  hashed into shape.
- **No passphrase KDF.** Accepting a passphrase invites `hunter2` and then requires an
  Argon2 parameter conversation for something no human needs to type. `sighop keys secret`
  generates one from the system CSPRNG and prints it once, next to the sentence that losing it
  makes every stored identity unrecoverable.
- **Never logged, never echoed, never stored in the database** — the last of which is what makes
  §6's "a DB dump must not be sufficient to impersonate a room server" actually true.

Poly1305 gives the authentication tag the spec asks for, so a tampered `sealed_seed` fails loudly
instead of yielding some other key. On load, the public key derived from the decrypted seed is
compared against the stored `public_key` column, which catches the remaining ways a row can be
wrong.

*Alternatives rejected:* AES-256-GCM via `cryptography` (equally sound, also already a dependency,
but the caller owns nonce generation and there is no upside to buy that risk); `pgcrypto` (server-side
encryption puts the key in the query and the server's log, exactly backwards); and a KEK/DEK
envelope scheme (correct for key rotation at scale, unjustified for 2–5 identities — the version
byte leaves the door open).

### D5 — Migrations are the only schema authority, and the runtime refuses to apply them

`alembic upgrade head` is a deliberate act (`sighop db upgrade`). `sighop run` compares the
database's applied revision against the revision compiled into the code and **refuses to start**
when they differ, naming both and the command that fixes it.

The alternative — auto-migrating on startup — is what turns a rollback into an outage: an old
binary restarted after a failed deploy meets a schema it does not know, and a new binary racing
another instance applies DDL twice. Refusing is one line of operational friction and removes the
whole class.

`create_all()` is not used anywhere, including in tests. Tests run the same migrations the
deployment runs, which is the only way the migration chain is actually exercised before it meets
real data.

### D6 — Pool sized to 10 against a role limit of 30, with `pool_pre_ping`

Measured: `rolconnlimit = 30`. One instance defaults to `pool_size = 5, max_overflow = 5` — at
most 10 — which leaves room for a second instance, an Alembic run, a `psql` session and the test
suite without anyone hitting `FATAL: too many connections for role`. That error was observed
during the probe for this design, which is why it is in the spec as a distinct failure from an
unreachable server: the remedy is different and the message must not conflate them.

`pool_timeout` is bounded (10 s) so pool exhaustion surfaces as a reported error rather than an
unbounded wait, and `pool_pre_ping` is on because the database sits behind a NodePort on another
host and idle connections through that path will be reaped.

There is nothing in this milestone that needs concurrency: one writer task per store and one
pruner. The pool is sized for headroom, not for parallelism.

### D7 — `TIMESTAMPTZ` everywhere, UTC in the application, naive datetimes rejected

The dev server's `TimeZone` is `Europe/Helsinki`. That makes the usual advice concrete: a
`TIMESTAMP WITHOUT TIME ZONE` column here would silently record local wall-clock time, and the
same code against a UTC server would record something else — a bug that only appears in one
deployment and looks like a clock problem.

Every timestamp column is `TIMESTAMPTZ`, every value crossing the boundary is timezone-aware UTC,
and a naive `datetime` offered for storage raises rather than being assumed. This matters more than
usual because the corpus, the capture headers and the dedup TTL all reason about instants across
sessions recorded on different days.

### D8 — Database failure is contained at the repository boundary, and visible

Repository calls used by the pipeline return an outcome; they do not raise into the RX path, the
scheduler or the bus. A failed write increments a counter, emits an `error`-level wide event
naming the operation, and flips a `degraded` flag that the periodic status line reports —
distinctly from "persistence off", because "off" is a choice and "degraded" is a fault.

Recovery needs no restart, and it is **not** waited for: a flag that cleared only when some write
happened to succeed would keep reporting `degraded` for hours on a node whose adverts are 24 h
apart. D15 gives the degraded state its own probe, and that probe is what clears the flag and
triggers the backfill. This is the same posture §4.1 takes toward the serial link: reconnect, keep
going, and make the gap visible rather than pretending it did not happen.

### D9 — `config.py` reads the environment; `uv run --env-file` reads the file

§11 has listed `src/sighop/config.py` since the beginning and nothing has needed it until now. It
gains exactly two settings plus their tuning knobs: `DATABASE_URL` and `SIGHOP_SECRET_KEY`.

**No dotenv dependency.** `uv run --env-file .env.dev sighop run …` already loads the file, and the
container takes its environment from compose or Docker secrets (§10). Adding a library to read a
file that two existing tools already read would be a dependency bought for nothing.

`.env.dev` stays gitignored. A committed `.env.example` documents the variables, their formats and
the fact that `SIGHOP_SECRET_KEY` is generated by `sighop keys secret` — so the required
configuration is discoverable without the secrets being in the repository.

### D10 — `db/` is a peer of `net/`, and the import boundary test grows a rule

`protocol/` has no dependency on `db/` or `net/` (§11) and `tests/protocol/test_import_boundary.py`
proves it. That test is extended to assert the new direction explicitly — `protocol/` imports
nothing from `db/` — rather than relying on `db/` not existing at the time it was written.

`db/models.py` holds table definitions and `db/repositories.py` the operations `net/` calls.
`net/contacts.py` and `net/paths.py` accept an optional repository and are unchanged in every other
respect; neither imports SQLAlchemy. That keeps their existing tests running with no database and
makes the persistent path a thin, separately testable adapter.

### D11 — Tests isolate in a throwaway **schema**, because the role cannot create a database

The measured `rolcreatedb = false` rules out the usual per-run test database. The role does have
`CREATE` on the database, so each database test session creates `sighop_test_<random>`, sets the
search path to it, runs the real migration chain into it (with Alembic's version table in the same
schema), and drops it with `CASCADE` at the end.

That is arguably better than a throwaway database: it is faster, it exercises the migrations, and
it cannot collide with another developer's run. Its one hazard — a crashed run leaving a schema
behind — is handled by naming them with a common prefix and dropping stale ones at session start.

Database tests **skip** when no test database is configured, and the pytest marker that gates them
is the same one CI can select. The existing suite gains no database dependency, which is the
condition the proposal set.

### D12 — Contacts and paths are upserted on their natural keys

`contact` is keyed on the public key, which is the identity (§3, and the existing store's own
rule); `ON CONFLICT (public_key) DO UPDATE` is the write. `path` is keyed on the tuple that makes
a candidate route distinct — destination, path bytes, hash size — so hearing the same route again
updates `confirmed_at` and `snr_db` rather than accumulating rows.

Storing every candidate rather than only the winner is deliberate: §13 unknown #3 (whether path
scoring needs more than most-recently-confirmed-wins) is still open *because* nobody has yet
observed two routes to one peer. Persisting candidates is how that observation eventually gets
made — the data accumulates across runs instead of being thrown away every restart.

### D13 — A replay run does not write

Replaying a capture produces receptions whose timestamps belong to an earlier session. Writing
them as though they had just been heard would put contacts in the store whose `last_heard` is a
week old but which look, to everything downstream, exactly like a peer heard this minute — and it
would let a test replay pollute the operator's real database.

So replay ignores the database and says so, with an explicit opt-in for the case where someone
genuinely wants to backfill from a capture. This is the same reflex as the transmit gate: the
destructive direction requires a deliberate act.

### D14 — Keyfiles keep working, and the burned fixture is untouched

`--entity <keyfile>` keeps working exactly as it does; a persistent run simply also loads what the
entity store holds, with the node-hash collision rule applied across both sources. Nothing forces
an operator to migrate, and `sighop keys import` is the one-function conversion milestone 4's D1
promised.

`tests/fixtures/burned-first-transmit.json` and `tests/protocol/test_foreign_decrypt.py` are not
touched by this milestone. The exit criterion of milestone 4 is a regression test precisely so it
keeps proving itself through later work; a persistence milestone that had to edit it would be
doing something wrong.

### D15 — Contacts unwritten during an outage are backfilled when the database returns

A dropped `path` row costs nothing — the next reception relearns it — and a dropped `packet_log`
row is a gap in a feed the design already calls disposable (D2). A dropped `contact` row is
different in kind: re-acquiring it means waiting for the peer to advert again, and the advert floor
is 24 h. That is the wait milestone 4 built `--peer-wait` around, and losing a contact to a
five-minute Postgres outage would reintroduce it in a window nobody is watching.

So each contact carries an in-memory **unpersisted marker**. A successful write clears it; a failed
one leaves it set. When the database returns, every marked contact is written before normal
operation resumes. The write is an upsert on the public key (D12), so a contact observed several
times during the outage lands its *latest* state once rather than replaying each observation.

The set is small enough that no cleverness is required — 92 adverts across the 1003-record corpus,
against a 2–5 entity target — so "write everything marked" is the whole algorithm, and it is
bounded by the size of the contact table rather than by the length of the outage.

**This needs a recovery trigger, and "the flag clears on the next successful write" is not one.**
With adverts as rare as they are, nothing may attempt a write for hours after the database returns,
so the backfill would not run and the status line would go on reporting `degraded` long after the
fault cleared — the monitoring equivalent of a stuck needle. While degraded, the runtime therefore
probes at a bounded interval (default 30 s, capped backoff), and a successful probe is what clears
the flag and runs the flush.

*Deliberately not extended to paths or the packet log.* Backfilling those means retaining rows the
design has already decided are disposable, turning a bounded queue into unbounded state for data
that either regenerates from the next reception or does not matter. The asymmetry is the point:
contacts are backfilled because they are the only store where the loss is expensive.

*Residual, stated rather than designed away:* a process killed **during** an outage still loses the
contacts observed in it. Backfill closes the recovery window, not the crash window. Closing that one
too would take a local write-ahead file — a second store to reason about, and a second thing to
corrupt, for a case a restart already tolerates.

### D16 — Every database operation is time-bounded, and no contact write happens inline

D2's first form wrote contacts through *on the bus subscriber that records verified adverts*. That
is safe only if a write cannot take long, and here it can: the database is a NodePort on another
host, and a host that is **down blackholes packets rather than refusing them**, so a connect does
not fail fast. asyncpg's default connect timeout is 60 s. A 60-second stall inside an advert
subscriber is a 60-second stall in a pipeline whose defining property is that replaying a capture
reproduces every reception exactly.

Two changes, and they are independent:

1. **Every operation is bounded.** An explicit connect timeout (default 5 s) and statement timeout
   (default 5 s) join the bounded `pool_timeout` from D6. Exceeding any of them is a reported
   database error that sets `degraded` and is retried by D15's probe — never an unbounded wait
   inside a caller.
2. **Contact writes leave the subscriber path.** They go to a dedicated single-writer queue, like
   paths and the packet log — but unlike those, this queue **never drops**. If it fills, the
   contact's unpersisted marker (D15) is what carries it, and the next flush writes it. Async does
   not become lossy; the overflow path degrades into the mechanism that already exists.

The result is that no database state — reachable, slow, or blackholing — can add latency to
decoding, dispatching or scheduling a packet.

*Alternative rejected:* keep the inline write and just shorten the timeout. Five seconds on a
subscriber is still five seconds, and adverts arrive in bursts after an outage, so the stall
compounds exactly when the system is already unhappy.

## Risks / Trade-offs

- **Write-behind loses the most recent state on a hard kill** (D2) → deliberate for paths and the
  packet log, whose rows are cheap to regenerate and explicitly not an audit trail. Contacts, the
  one store where re-acquisition is expensive, are written at once, never dropped, and backfilled
  on recovery (D15) — the residual is a kill *during* an outage, which D15 states rather than hides.
- **The unpersisted-contact marker is a second source of truth about what is stored** (D15) → it is
  advisory only: it can be wrong in the safe direction, because a redundant upsert on the public key
  is idempotent. A marker wrongly set costs one extra write; a marker wrongly cleared is what the
  tests target.
- **The degraded probe adds a periodic connect attempt against a database that is down** (D15) →
  bounded interval with capped backoff, one connection, and it is the same probe that clears the
  flag. Without it the flag is a stuck needle and the backfill never fires.
- **The `degraded` state is easy to build and easy not to notice** (D8) → it is in the periodic
  status line as a distinct value from "off", it carries the discard counters, and there is a test
  that asserts the line differs between the two.
- **Losing `SIGHOP_SECRET_KEY` loses every stored identity** (D4) → irreducible; that is what
  encryption at rest means. Mitigated by `sighop keys export`, which §6 asks for anyway, and by
  the generation command printing the warning at the only moment anyone is listening.
- **A shared development database means two developers' runs interleave** → the dev database has
  one consumer today, the connection budget (D6) leaves room for a second, and D11's per-run
  schemas keep tests from ever touching application tables.
- **The initial migration will be wrong about something** → it is the first migration against an
  empty database, so being wrong costs a second migration rather than a data conversion. This is
  the cheapest moment in the project's life to be wrong about DDL, which is part of why the scope
  is four tables and not eight.
- **`packet_log` grows faster than expected on a mesh busier than any recorded** → the measured
  rate is ≈191 receptions/hour, so the default cap (100 000 rows) is roughly three weeks; per-file
  traffic in the corpus varies by an order of magnitude, so the cap is enforced by row count rather
  than by age, and the pruner runs on a schedule rather than on a threshold nobody watches.
- **asyncpg's stricter URL handling breaks a deployment that worked in development** (D1) → the
  config module validates the URL's driver and query parameters at startup and says what it will
  not accept, rather than letting the failure surface as a driver exception at first connect.
- **A database that is present but at the wrong revision blocks startup** (D5) → intended, and the
  error names both revisions and the command. The alternative silently corrupts.

## Migration Plan

There is no data to migrate — the database is empty and this is the first schema. The sequence is:

1. `uv add` the three dependencies; `uv lock`.
2. Write `alembic/env.py` (async, D1) and the initial migration (D3). Apply it to the dev database
   with `sighop db upgrade` and confirm `sighop db current` agrees with the code.
3. `sighop keys secret` → put it in `.env.dev` beside the rewritten `postgresql+asyncpg://` URL.
4. `sighop keys import` the existing development keyfile; confirm `sighop keys list` shows it and
   that `sighop run` loads it from the store with the same public key and node hash.
5. Run receive-only against the live mesh long enough to accumulate contacts, paths and packet-log
   rows; restart; confirm the counts come back and that the startup line reports them.
6. Kill the process without a graceful stop and confirm contacts survived (D2, D15) and that the
   run reports what it discarded.
7. Exercise the outage path deliberately: with the runtime receiving, make the database
   unreachable for several minutes, confirm reception and scheduling are unaffected and the status
   line reads `degraded`; then restore it and confirm the probe clears the flag and backfills the
   contacts observed meanwhile, without a restart (D15, D16).

**Rollback** at any point is unsetting `DATABASE_URL`: the runtime returns to in-memory operation
with no code change, which is the same property that makes the whole milestone low-risk. Schema
rollback is `alembic downgrade`, and the initial migration's `downgrade()` drops the four tables —
tested, because an untested downgrade is a downgrade that does not exist.

## Open Questions

1. **Should `path` persist every candidate or only the resolved winner?** D12 says every
   candidate, on the grounds that §13 unknown #3 needs the observation. If the row count turns out
   to grow without bound on a busy mesh — the existing in-memory bound is on destinations, not on
   candidates per destination — a per-destination candidate cap is the answer, and it needs a
   measurement this milestone will produce.
2. **The `packet_log` retention default** (100 000 rows) is derived from one 2 h 54 min session at
   ≈191 receptions/hour. Per-file traffic in the corpus varies by an order of magnitude, so this is
   a starting point to be re-derived from the first long persistent run, exactly as the dedup TTL
   was.
3. **§13 unknown #4 — does a peer that learns us only from a zero-hop advert flood its replies?**
   Not settled here and not a goal, but durable contacts make it cheap to test: the exchange no
   longer has to complete inside one process lifetime. DESIGN.md names milestone 5 *or* 6 for it;
   if a natural opportunity arises during step 5 of the migration plan, take it and record it.
4. **Does the WebUI want the packet log indexed differently?** Milestone 8 owns its queries. The
   indexes here serve pruning and packet-identifier lookup, and adding one later against a bounded
   table is cheap.
