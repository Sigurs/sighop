## Why

Milestone 8 shipped a panel that can open the transmit gate, raise a legal duty-cycle ceiling
and hand out private seeds, with no authentication in front of any of it. That gap was made
explicit, defaulted to loopback and announced — and it was always owed to this milestone.
DESIGN.md §12 names milestone 9 in four words: **container, compose, build script, auth**. None of
the first three exist yet (there is no `Dockerfile`, `compose.yaml` or `build.sh` in the
repository, though §10 and §11 have described all three since the beginning), and until they do,
running sighop means a checkout, `uv`, and an operator who remembers which environment file to
pass.

The two halves belong together. A container is the first deployment in which sighop *cannot* bind
loopback and be reachable — inside it, the panel has to listen on a non-loopback address — so
shipping the image before authentication would turn milestone 8's loud, opt-in exception into the
default way to run the platform.

## What Changes

- **Every page, form and the feed's WebSocket require a signed-in user.** Username plus Argon2id
  password hash (the `passwords.py` hasher room logins already use, off the loop and bounded),
  a session cookie that is `HttpOnly` and `SameSite=Strict`, and a default-deny rule: a route is
  public only by being named on a short allowlist (the login form and the static assets). There
  is **no option that turns authentication off**, on loopback or anywhere else.
- **BREAKING: `--web` requires a database.** Accounts live in a new `web_user` table (migration
  `0005`). A run asked for the panel with no database configured, or with no enabled account, is
  a startup failure naming the command that fixes it. This retires milestone 8's "the panel works
  with no database" scenarios; a run *without* `--web` still needs no database.
- **Accounts are managed from the terminal only.** `sighop web user add|list|passwd|disable|enable|remove`.
  Passwords come from a prompt or standard input, never argv — the rule rooms already follow.
  The panel names account management as a capability it deliberately does not offer, beside
  migrations and secret generation.
- **Sessions are in memory, bounded, and expire.** Idle and absolute lifetimes, a new session id at
  every login, logout as a POST, and revalidation against `web_user` on a short interval so a
  password change or a disable made from another process ends that user's sessions within the
  bound. A restart signs everyone out.
- **Login is throttled and says nothing useful to a guesser.** Per-account and per-client failure
  backoff; an unknown username costs the same Argon2id verification as a wrong password; one wide
  event per attempt carrying the outcome, never the password.
- **§8's guarded actions are re-authenticated and name who did them.** Revealing or exporting a
  private key, enabling transmit and raising the ceiling now require the acting user's password
  on the confirmation form in addition to the one-shot nonce. `web_guarded_action` and
  `web_request` carry `actor` as the signed-in username instead of `unauthenticated`. Posting to a
  room stays confirm-and-nonce, now with an actor.
- **Plain HTTP, and the non-loopback warning says what that means now.** sighop does not terminate
  TLS and still does not trust a reverse proxy. A non-loopback bind remains permitted and
  announced; its warning changes from "no authentication" to "passwords and session cookies cross
  the network unencrypted — reach this over a tunnel". §8's "secure cookie" is corrected rather
  than set on a cookie that is only ever served over HTTP.
- **Host names beyond the bind address can be allowed.** A panel bound to `0.0.0.0` inside a
  container is reached as `localhost:8080`, which milestone 8's rebinding check refuses because it
  only accepts the literal bind address. `--web-allowed-host` (repeatable) names the others.
- **A container image.** Multi-stage `Dockerfile`: `uv sync --frozen --no-dev --no-editable` in a
  build stage, only the virtual environment, the migration chain and the application in the final
  stage, no hardcoded UID, bytecode compiled at build so a read-only root filesystem needs no
  writable cache, version and commit baked in as labels and as `SIGHOP_COMMIT_HASH`.
- **A compose deployment.** `sighop` under `user: "${UID}:${GID}"` with the host's `dialout` GID
  added, the modem mapped by `/dev/serial/by-id/…` to `/dev/modem`, `read_only`, a `/tmp` tmpfs,
  `cap_drop: [ALL]`, `no-new-privileges`, the panel published on host loopback only; Postgres on an
  internal network with no published port. Two services only: `sighop` starts with `run --migrate`,
  which applies outstanding migrations before the schema check (operator decision, replacing a
  separate one-shot migration service), and secrets come from the gitignored `.env`.
- **A build script.** `build.sh`: lint, typecheck, test, image build, a smoke run of the built image
  as an arbitrary UID on a read-only root, and a vulnerability scan that fails the build on fixable
  high and critical findings.

## Capabilities

### New Capabilities

- `web-auth`: accounts, login, sessions, logout, throttling, the default-deny route rule, the
  WebSocket's authentication, re-authentication for guarded actions, and the acting user on every
  event.
- `container-image`: what the image contains and does not, how it runs as an arbitrary UID on a
  read-only root, where its version and commit come from, and where it finds its migrations.
- `compose-deployment`: the reference deployment — device access, privilege dropping, network
  isolation of the database, secret delivery, and migration on start.
- `build-script`: the ordered gates `build.sh` runs and what makes it fail.

### Modified Capabilities

- `web-server`: the non-loopback warning's content; additional allowed host names; a run with no
  database can no longer serve the interface, replacing the "no database configured" degradation
  scenario; provenance tokens become per-session.
- `web-admin`: guarded actions require the acting user's password and record the actor; account
  management joins the capabilities the interface names as deliberately absent.
- `web-chat`: "usable with no database configured" is removed — the panel cannot run without one —
  while "usable when the database is degraded" stays.
- `runtime-cli`: `--web` requires a database and an enabled account; `--web-allowed-host`; the
  non-loopback startup statement; the `sighop web user` command surface; web passwords follow the
  never-in-argv rule; `run --migrate`.

## Impact

- **New**: `src/sighop/web/auth.py` (accounts, sessions, throttle, the auth middleware),
  `src/sighop/web/routes/session.py` (login, logout), `templates/login.html`,
  `alembic/versions/0005_web_users.py`, `WebUserRepository` in `db/repositories.py`, `Dockerfile`,
  `.dockerignore`, `compose.yaml`, `build.sh`, `.env.example` additions.
- **Modified**: `web/app.py` (auth wiring, allowed hosts, startup wording), `web/guard.py`
  (per-session provenance, actor on the request event), `web/guarded.py` (password
  re-authentication, actor), `web/routes/keys.py` and `routes/admin.py` (confirmation forms),
  `web/templates/` (sign-out, confirmation password field, absent-capability note), `cli.py`
  (`web user` noun, `--web-allowed-host`, startup refusals),
  `db/models.py`, `tests/webfixtures.py` (a signed-in client; no auth-off path in production code).
- **DESIGN.md**: §8 (authentication as built, the cookie correction, plain HTTP), §10 (as built),
  §11 (`auth.py`, deployment files), §12 (milestone 9 record).
- **Dependencies**: none added to the application. The scanner runs from a pinned container image
  in `build.sh`, not as a project dependency.
- **Unchanged**: `protocol/`, `net/`, `bots/` and `radio/` gain nothing; the corpus replay's counts
  stay byte-identical. `run` without `--web` behaves exactly as before, database or not.
- **Security posture**: closes the unauthenticated-panel gap milestone 8 opened. Leaves one stated
  gap open by operator decision: traffic to a non-loopback bind is unencrypted, and the startup
  warning says so.
