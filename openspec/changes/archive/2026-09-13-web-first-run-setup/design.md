## Context

Milestone 9 (archived `2026-09-13-milestone-9-hardening`, design D2) made accounts terminal-only
and made `run --web` refuse a database with no enabled account (`NO_ENABLED_ACCOUNT`, raised in
`cli._attach_web` and again in `WebInterface.bind`). In compose that refusal is a restart loop
until `docker compose run --rm -it sighop web user add <name>` is run.

Constraints that shape the approach:

- **Default-deny guard.** `web/guard.py` `PUBLIC_ROUTES` is the whole unauthenticated surface;
  `tests/test_web_auth_routes.py` walks the route table against it. Non-safe methods need a
  provenance token — the process-wide `Authenticator.login_token` before a session exists.
- **Client address is useless for trust in compose.** Every request arrives from the Docker
  gateway address, so "loopback clients only" cannot distinguish the operator from the network,
  and the per-client throttle is already effectively global.
- **Human output and events are separate streams of intent.** `WebInterface.startup_lines()` is
  printed to the run's output; `report()` emits `web_interface_listening`. Both reach
  `docker compose logs`, but only events are meant for machines and log shipping.
- **`Database.run` is one transaction per unit of work** (session + commit), Postgres only.
- **`PasswordHasher`** already bounds Argon2id work off the event loop; the panel has its own.

Operator decision for this change: setup is protected by a one-time code shown in the run's
output (rejected: code from an environment variable; first visitor wins).

## Goals / Non-Goals

**Goals:**
- An empty `web_user` table plus `run --web` yields a usable panel with no terminal step beyond
  reading the logs.
- A network client without the code cannot create an account, and cannot learn anything about
  setup beyond "setup is pending".
- At most one account is ever created through setup, and never beside an existing one.

**Non-Goals:**
- Any other browser account management (add second account, change password, disable, list).
  Milestone 9 D2's reasoning is unchanged for those.
- Re-opening setup while the process runs (e.g. after every account is removed from a terminal
  mid-run). Setup is decided at startup; a restart re-decides it.
- Emailing, QR codes, or any out-of-band delivery of the code.

## Decisions

### D1. Setup is decided at startup, from total account count, not enabled count

`_attach_web` reads a new `WebUserRepository.count()` beside `count_enabled()`:

| total | enabled | result |
|-------|---------|--------|
| 0     | 0       | serve in setup mode, generate code |
| ≥1    | 0       | fail, `NO_ENABLED_ACCOUNT` reworded to name `web user enable <username>` and `web user add <username>` |
| ≥1    | ≥1      | unchanged |

All-disabled means an operator deliberately locked the panel (`--allow-no-accounts`); setup must
not be a way around that. `WebInterface.bind` keeps its own refusal but accepts
`accounts_enabled == 0` when handed a setup object.

Rejected: deciding per request from the live count (setup could re-open mid-run after a terminal
`remove`, with a code that has already been sitting in logs for days).

### D2. `FirstRunSetup` lives on the `Authenticator`

A small object in `web/auth.py`: the code, a `pending` flag, `check(submitted) -> bool`,
`close()`. `Authenticator` gains `setup: FirstRunSetup | None = None` and a
`complete_setup(...)` decision method beside `sign_in`. The guard already holds the authenticator,
so it reads `auth.setup.pending` to choose the redirect target without a new constructor argument,
and "every authentication decision in one place" stays true.

`AccountStore` (the structural protocol, milestone 9 D16) gains `count()` and `add_first()`.
This is the protocol's first write; it is added there rather than as a second protocol because
`MemoryAccounts` in `tests/webfixtures.py` already implements the store and a second seam would
be a second answer to "where do accounts come from".

### D3. The code: 20 Crockford base32 characters, 100 bits, shown grouped

`secrets.choice` over `0123456789ABCDEFGHJKMNPQRSTVWXYZ`, 20 characters, displayed
`XXXXX-XXXXX-XXXXX-XXXXX`. Input is normalised by upper-casing and removing `-` and whitespace,
then compared with `hmac.compare_digest` on the ASCII bytes. Crockford's alphabet omits I, L, O
and U so a code read off a terminal is not mistyped as a different valid code; no I→1 / O→0
folding is done, since those letters never appear.

Rejected: `token_urlsafe` (mixed case and `-`/`_` are hostile to reading off a log and typing);
a 6-digit PIN (needs a throttle to be safe, see D5).

### D4. The code is printed in `startup_lines()` only

Setup-mode startup output:

```
web: http://127.0.0.1:8080 — FIRST-RUN SETUP PENDING: no account exists
     open http://127.0.0.1:8080/setup and enter setup code 7KQ2M-X9D4R-P0TZH-3VW8N
     reachable from this host only
```

`web_interface_listening` gains `setup_pending: true` and `accounts_enabled: 0`, never the code.
Setup outcome events (`web_setup`, same fields and level convention as `web_login`: `outcome`,
`reason`, `username`, `client`) never carry the submitted code or password. On completion the run
also announces through `runtime.say` — "web: first-run setup completed; account 'x' created" — so
the terminal operator sees it as they see guarded actions.

For a wildcard bind (`0.0.0.0`) the URL printed is the bound address, as today; the operator
substitutes the published host name. Not worth inventing host discovery for.

Rejected: logging the code in the event (events are the stream most likely to be shipped to a
third-party store); writing it to a file in the container (read-only root, and one more place to
clean up).

### D5. No throttle on wrong codes

A wrong or missing code is refused before any Argon2id work, so refusals cost a constant-time
compare. At 100 bits, online guessing is not a threat at any request rate the process can serve.
A throttle would add nothing and would hand anyone on the network a way to lock the operator out
of setup for up to 15 minutes — in compose all clients share one address, so a per-client delay
is a global one. Every refusal is still an event.

Order inside `POST /setup` (after the guard's host and provenance checks):

1. `setup` absent or closed → refuse `setup_closed`, render redirect to `/login`.
2. Code wrong → refuse `bad_code`: fixed message, username not echoed, nothing hashed.
3. Username fails `normalise_username` → refuse `bad_username`, message from `UsernameError`.
4. Passwords empty or differ → refuse `password_empty` / `password_mismatch`.
5. Hash password via `auth.hasher` (off loop, bounded).
6. `add_first` → if it reports accounts exist, `close()` and refuse `setup_closed`.
7. `close()`, issue session (`SessionStore.issue`, replacing any presented cookie), set cookie
   with the same attributes as `/login`, `303` to `/`.

Steps 2–6 hold a per-process `asyncio.Lock` so a double-clicked submit hashes once and the second
request sees setup closed. The lock is a courtesy; D6 is the guarantee.

### D6. `add_first` is atomic in one transaction under a table lock

```sql
LOCK TABLE web_user IN SHARE ROW EXCLUSIVE MODE;
SELECT count(*) FROM web_user;          -- > 0 → return "exists", insert nothing
INSERT INTO web_user (...) VALUES (...);
```

inside one `Database.run` unit. `SHARE ROW EXCLUSIVE` conflicts with itself and with the
`ROW EXCLUSIVE` lock an ordinary `INSERT` takes, so two setups in different processes serialise,
and a terminal `web user add` either commits first (setup sees count 1) or waits until setup
commits (and then succeeds as a second account, which is fine — the terminal is trusted). Returns
`Outcome[WebUserRecord | None]`, `None` meaning an account already existed.

Rejected: `INSERT … WHERE NOT EXISTS` without a lock (under READ COMMITTED two concurrent
statements both see an empty table); an advisory lock (the CLI would have to take it too, and a
forgotten caller silently breaks the guarantee).

### D7. Routes and redirects

- `GET /setup`, `POST /setup` added to `PUBLIC_ROUTES`, in a new `web/routes/setup.py`.
- `GET /setup`: if `setup` is absent or closed → `303 /login`. Otherwise read `accounts.count()`;
  if > 0 (added from a terminal), `close()` and `303 /login`; if the read fails, render the form
  anyway — the submission's `add_first` is the real check. Else render `setup.html`.
- Guard: an unauthenticated safe request to a non-public path redirects to `/setup` while
  `auth.setup` is pending, else `/login?next=…` as today. `next` is not carried to `/setup`;
  setup always lands on `/`.
- `GET /login` while setup is pending → `303 /setup`, so a bookmarked sign-in page does not show a
  form nobody can use. `POST /login` is unchanged and fails as an unknown user naturally.
- `setup.html` is standalone like `login.html`: process `login_token`, fields `setup_code`,
  `username`, `password`, `password_again` (`autocomplete="new-password"`), and a note pointing at
  `docker compose logs sighop` / the run's output and at `sighop web user add` as the alternative.

### D8. `web user` CLI wording

`_leaves_no_account` distinguishes "last enabled, others disabled" (existing `NO_ACCOUNT_LEFT`)
from "last account at all" (new `NEXT_RUN_OFFERS_SETUP` message). `web user list` with no accounts
states that `run --web` will offer first-run setup, or `web user add <username>` from a terminal.
The `--allow-no-accounts` flag and exit codes are unchanged.

## Risks / Trade-offs

- [Code visible to anyone who can read container logs] → that is the `docker` group, already
  root-equivalent; the code dies at setup completion or restart, and never enters structured
  events.
- [Run output shipped to a log aggregator exposes the code] → only until setup completes; the
  completion announcement makes an unexpected completion visible; operators who ship raw stdout
  can use `web user add` instead, which remains documented.
- [Removing the last account on a network-exposed deployment re-opens setup at next restart] →
  refused without `--allow-no-accounts`, and the refusal and the acknowledged removal both state
  the consequence; the code is still required.
- [Setup served on `0.0.0.0` over plain HTTP sends the code and new password unencrypted] → same
  exposure as sign-in, already warned at startup and unchanged; the warning is printed beside the
  code.
- [`AccountStore` stops being read-only] → one narrowly named write (`add_first`) that can only
  succeed on an empty table; no general `add` is exposed to `web/`.
- [A stale setup page submitted after a terminal add] → `add_first` returns "exists", refusal
  recorded, browser sent to sign-in.

## Migration Plan

No schema migration, configuration or dependency. Existing deployments with accounts see no
change. A deployment currently restart-looping on an empty table starts serving setup after the
upgrade. Rollback: revert; an empty table goes back to refusing startup, and an account created
through setup remains an ordinary account.
