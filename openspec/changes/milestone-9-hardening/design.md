## Context

See proposal.md — Why. Three facts about the code as it stands shape everything below.

- **The panel already has a guard, and the guard is the right place for authentication.**
  `web/guard.py`'s `RequestGuard` is plain ASGI middleware that checks the `Host` header before any
  handler runs, checks a per-process provenance token on every unsafe method, and emits the one
  `web_request` event per request. `web/guarded.py` adds confirm-then-act with a one-shot nonce and
  `audit()`, whose `actor` field is hardcoded to `"unauthenticated"` with a comment saying milestone
  9 fills it in. Milestone 8 built both to survive users arriving; this design keeps that promise
  rather than adding a second middleware stack.
- **Argon2id is already solved.** `passwords.py` wraps libsodium's `argon2id.str()`/`verify()` in
  `PasswordHasher`, which runs each operation in a worker thread behind a semaphore (64 MiB each,
  bound 2) so a login storm cannot stall the radio's loop. Room logins use it today.
- **A container breaks two of milestone 8's assumptions.** Inside a container the panel must bind a
  non-loopback address, and `allowed_hosts()` then admits only that literal address — so a browser
  reaching the published port as `localhost:8080` is refused as a rebinding attempt. And
  `migrations_dir()` finds `alembic/` beside the source checkout or in the working directory; a
  non-editable install in an image has neither unless `SIGHOP_ALEMBIC_DIR` says where.

Operator decisions taken before this design (not re-litigated here): accounts live in the database
and `--web` requires one; sighop serves plain HTTP only, never terminating TLS; milestone 8's
leftover defects (frozen feed status line, reload not retiring a nonce, the 5-byte keep-alive
acknowledgement) stay out of this change.

Measured before writing: the host has Docker 29.7.2 and no scanner, `hadolint` or `shellcheck`
installed; `ruff format --check` would reformat 72 of 152 files, so formatting is **not** an
established gate and `build.sh` does not add one; `chat/_messages.html` polls every 3 s.

## Goals / Non-Goals

**Goals:**

- No request reaches platform state without a session, and the rule is default-deny: forgetting to
  protect a new route leaves it protected.
- No production code path serves the panel unauthenticated — including one "only for tests".
- Nothing on the reception, dedup, dispatch or transmit path changes; the corpus replay's counts
  stay byte-identical, and `run` without `--web` is untouched.
- `build.sh` exiting 0 means an image exists that passed lint, types, tests, a constrained smoke run
  and a scan — nothing less.
- The compose file is the deployment §10 describes, and every deviation from it is written down.

**Non-Goals:**

- Roles or per-user permissions. Every account is an operator; §8 names no second kind of user,
  and a role model built without one would be a guess.
- Account management, password change or self-service in the browser (see D2).
- TLS termination, reverse-proxy trust, `X-Forwarded-*` (operator decision; §8).
- Durable sessions, "remember me", multi-process session sharing.
- Multi-architecture images. `linux/amd64` only; see Open Questions.
- Image publishing to a registry, signing, SBOM attestation.

## Decisions

### D1. Accounts are a `web_user` table in migration `0005`

Columns: `id`, `username` (text, stored lower-cased, `UNIQUE`), `password_hash` (the encoded
`$argon2id$…` string, parameters and salt included), `enabled`, `created_at`,
`password_set_at` (all `TIMESTAMPTZ`, UTC, per §6). `WebUserRepository` beside the others in
`db/repositories.py`, reached through `Persistence`.

`password_set_at` doubles as the credential epoch: a session records the value it was issued
under, and revalidation (D4) ends it when the row's value differs. That avoids a separate
`session_generation` counter that could drift from the thing it describes.

Username normalisation is `str.casefold()` after NFKC, refusing empty, whitespace and control
characters and capping at 64 characters. Rejected: `citext` — an extension the measured role may
not be able to create, for a comparison one normalisation function does.

The migration's docstring states that a downgrade deletes every account, and that a run with
`--web` then cannot start — discovered from the docstring rather than from a failed start.

Rejected: an environment-supplied bootstrap credential alongside the table (operator decision), and
accounts in a mounted file (no revocation that reaches a running process without re-reading it).

### D2. Accounts are managed from the terminal only

`sighop web user add|list|passwd|disable|enable|remove`, a new `web` noun in `cli.py` reusing the
room commands' password input (prompt without echo, twice on a TTY, or one line on stdin; a
password-looking positional refused). `disable`/`remove` of the last enabled account needs
`--allow-no-accounts`.

The browser gets none of it, and `web-admin`'s absent-capabilities page says so. The reason is the
same shape as migrations: with no roles, anyone signed in could create a second account for
themselves, and a stolen session would become a persistent credential. Terminal access to the host
is a stronger proof of being the operator than a cookie. Rejected: "change my own password" in the
browser — it is the one safe subset, but it still lets a stolen session lock the owner out, and
it can be added later without disturbing anything.

### D3. Sessions live in memory, keyed by a hash of the cookie

`web/auth.py`'s `SessionStore`: a dict from `sha256(token)` to `Session(username, issued_at,
last_seen, password_set_at, csrf_token, verified_at)`. The cookie carries the raw
`secrets.token_urlsafe(32)`; the store never holds it, so a `repr`, a traceback or a heap dump of
the store yields nothing a browser can present.

Bounds: `MAX_SESSIONS = 256`, oldest evicted first; idle limit **12 h**; absolute lifetime **24 h**.
Idle is generous deliberately: the panel is an instrument that sits open beside other radio
tooling (§8), and a shorter idle would sign an operator out mid-watch. HTMX polling (the chat pane,
every 3 s) counts as activity, which means an open chat tab never idles out — the absolute lifetime
is the bound that always applies, and it is why there is one.

A restart signs everyone out. Rejected: a `web_session` table — sessions would survive a restart,
but each request would read the database, and during an outage every signed-in operator would be
locked out of the one tool that shows them the outage. In memory, an outage costs new sign-ins and
guarded actions (D4), not the dashboard.

Rejected: signed stateless cookies. Logout and revocation would need a server-side deny-list
anyway, which is a session store with extra steps.

### D4. Revalidation at most once a minute, fail-soft for reads and fail-closed for guarded actions

On a request, a session whose `verified_at` is older than `REVALIDATE_SECONDS = 60` re-reads its
`web_user` row (bounded by the engine's existing statement timeout). Row absent, disabled, or
`password_set_at` changed → the session ends, a `web_session_ended` event names the reason, and the
request proceeds as unauthenticated. This is how a `sighop web user disable` in another process
reaches a running panel with no IPC.

If the read fails because the database is degraded, the session is kept, marked unverified, and
retried on the next request. Pages keep working; every guarded action refuses with "this account
cannot currently be verified". The re-authentication for a guarded action (D7) reads the row fresh
anyway, so the fail-closed half costs nothing extra.

### D5. Authentication lives in `RequestGuard`, default-deny by path

The guard's order becomes: host check → resolve session from cookie → **public-path check** →
provenance → handler. One middleware, so the single `web_request` event can carry `actor` and every
refusal is seen by the same code.

`PUBLIC_PATHS = {("GET", "/login"), ("POST", "/login")}` plus the `/static/` prefix. Anything else
without a session: a safe method gets `303` to `/login?next=<path>` (the `next` value accepted only
as a same-origin absolute path, never a URL); an unsafe method gets `401` with no body worth
reading. Static files are audited by test to carry no template-rendered content.

Matching is by path in the ASGI scope rather than by resolved route, because routing happens inside
the application and the guard runs before it. The consequence is tested instead of trusted:
`tests/web` walks `app.routes` (the `registered_routes` helper milestone 8 already has) and asserts
every non-public route refuses a request with no session — which is what makes "a newly added
route" in `web-auth` a scenario rather than a hope.

The WebSocket path gets the same session lookup plus an `Origin` check against the allowed hosts,
closing with `1008` before `accept()`. `SameSite=Strict` already withholds the cookie from a
cross-site handshake; the `Origin` check is the second, independent reason.

### D6. Provenance tokens become per-session; the login form keeps a per-process one

Milestone 8's token was per process and embedded in every page. With accounts, a per-process token
readable by anyone who can load the (public) login page is weaker than it needs to be. A signed-in
page now carries the session's own `csrf_token`; the login form carries the process token, which is
all a pre-session request can be bound to. A token from another session is refused like no token.
The header/form-field mechanics, the 1 MiB body bound and the `Sec-Fetch-Site` check are unchanged.

Login rotates the session: the old cookie, if any, is dropped from the store before the new one is
issued (session fixation).

### D7. Guarded actions take the acting user's password; room posts do not

The confirmation forms for `reveal_private_key`, `export_private_key`, `enable_transmit` and
`raise_airtime_ceiling` gain a password field. Spending the action requires, in order: a session,
the session token, the nonce for that action and target, then `PasswordHasher.verify` against a
**fresh** row read. A wrong or missing password is refused as the action's own event and fed to the
login throttle under that username, but does not end the session — a typo should not cost the
operator their open panel.

`post_to_room` stays confirm-and-nonce. §8's list of re-authenticated actions is reveal, transmit
and ceiling; export joins because it is reveal as a file. A room post is irreversible but it is
content, not a change to what the station is permitted to do or who it can impersonate, and a
password prompt on every post would train operators to type their password without reading.

`audit()` gains a **required** `actor` keyword with no default, so a call site that forgets it is a
type error rather than an event that silently says `unauthenticated`.

### D8. Sign-in: one response for every failure, equal work, throttled twice

`POST /login` normalises the username, consults the throttle, then verifies: against the row's
hash if one exists, or against `DUMMY_HASH` — a hash of a random value computed once at startup
through the hasher, off the loop — if not. Disabled accounts verify their real hash and then fail.
Every failure returns the same page, same status (`200` with the form and one sentence), same
wording. The event distinguishes `unknown_user`, `disabled`, `bad_password` and `throttled`,
because the operator's log is trusted and the browser is not.

`LoginThrottle` keeps two LRU-bounded maps (`4096` keys each) — by normalised username and by
`scope["client"]` host — of consecutive failures and the time of the last. The first 5 failures
are free; after that each attempt must wait `min(2 ** (n - 5), 900)` seconds since the previous
failure, and one arriving early is refused as `throttled` **without** running Argon2id. A success
clears both entries. Unknown usernames are throttled exactly like real ones, so the throttle leaks
nothing about which exist.

A separate `PasswordHasher` instance for the panel, not the room server's: a burst of room logins
from the mesh must not queue an operator's sign-in, and vice versa. Peak Argon2id memory becomes
4 × 64 MiB, stated in the module.

The client address is the socket's own. Forwarding headers are ignored on purpose (§8): behind a
proxy every client shares one address and the per-address throttle degrades to a global one, which
is the safe direction.

### D9. The cookie is `HttpOnly; SameSite=Strict; Path=/` and is not `Secure`

`sighop_session`, no `Max-Age`/`Expires` (a browser-session cookie; the server's limits are the
authority). **Not `Secure`**, which corrects §8's "secure cookie": sighop serves plain HTTP by
operator decision, and a `Secure` cookie set over HTTP on anything but `localhost` is discarded by
the browser — sign-in would succeed server-side and loop back to the form, the kind of failure that
gets "fixed" by someone removing the check. The `__Host-` prefix requires `Secure` and is
unavailable for the same reason.

`SameSite=Strict` has one visible cost: following a link to the panel from another site arrives
without the cookie and shows the sign-in form to someone who is signed in. Accepted, because `Lax`
would send the cookie on a top-level cross-site GET, and the panel's safety posture already rests on
GET never changing state — Strict makes that a second wall instead of the only one.

### D10. `--web-allowed-host` extends, never replaces, the bind-derived set

Repeatable; each value is `name` or `name:port`, validated at startup (no `*`, no empty, no scheme,
no path). The final set is `allowed_hosts(host, port) ∪ extras`, each extra added both with and
without the bound port when given bare. The compose file passes `localhost:8080` and
`127.0.0.1:8080`. Nothing about the rebinding defence changes: an unnamed host is still `421`
before any handler.

### D11. The startup statement changes content, not prominence

Loopback: `web: http://127.0.0.1:8080 — sign-in required; 2 enabled account(s)`. Non-loopback adds,
unsuppressibly: `REACHABLE FROM THE NETWORK on 0.0.0.0 over plain HTTP: passwords and session
cookies cross the network unencrypted — reach this over a tunnel (SSH, WireGuard)`.
`web_interface_listening` gains `authenticated=true`, `encrypted=false`, `accounts_enabled`, and
drops `NO_AUTHENTICATION`. The account count is read in `_attach_web` before the bind, which is
also where the "no database" and "no enabled account" refusals happen — before a socket exists.

Inside the container this warning always fires, because the bind is `0.0.0.0`. That is correct:
the container's own network namespace is the "host" the process can see, and whether the published
port is loopback on the real host is compose's decision, not something the process can verify.

### D12. ~~`_FILE` variants for the two secrets~~ — removed

*(Built, then removed by operator decision.)* `DATABASE_URL_FILE` and `SIGHOP_SECRET_KEY_FILE` existed so
compose could deliver both as Docker secret files. Once compose took secrets from `.env` (D14) nothing
used them, and they were deleted with their tests rather than kept as an unexercised configuration
shape. One consequence of building them stayed: `keys import` and `keys export` had read
`SIGHOP_SECRET_KEY` from `os.environ` directly, and now read it through `Config` like `run`.

### D13. The image: `python:3.13-alpine` by digest, `uv sync --locked`, no user

Two stages on the **same** `python:3.13-alpine@sha256:…` base (originally `python:3.13-slim-trixie`; see below), so the virtual environment's
interpreter symlink resolves identically in both. `uv` is copied from a pinned
`ghcr.io/astral-sh/uv:<version>` image rather than installed. Build stage:
`UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
UV_PROJECT_ENVIRONMENT=/app/.venv`, then `uv sync --locked --no-dev --no-install-project` (the
dependency layer, cached across source changes), then the source and
`uv sync --locked --no-dev --no-editable`. `--locked`, not `--frozen`: `--frozen` installs from a
stale lock without complaint, which is exactly the `container-image` failure scenario.

Final stage copies `/app/.venv`, `alembic/` and `alembic.ini` to `/app`, sets
`SIGHOP_ALEMBIC_DIR=/app/alembic`, `PATH=/app/.venv/bin:$PATH`, `PYTHONDONTWRITEBYTECODE=1`,
`PYTHONUNBUFFERED=1`, `chmod -R a+rX /app`, sets **no** `USER`, and uses
`ENTRYPOINT ["sighop"]` with `CMD ["--help"]`. Build arguments `SIGHOP_VERSION` and
`SIGHOP_COMMIT` become OCI labels and `ENV SIGHOP_COMMIT_HASH`, which `logging.py` already reads.
Build arguments are safe to bake: they are a version and a commit, never a secret.

Every dependency with native code (`asyncpg`, `pynacl`, `cryptography`) ships a cp313 manylinux
wheel, so the build stage needs no compiler either; if a future dependency lacks a wheel the build
fails, which is the right signal for an image that promises no toolchain.

`.dockerignore` is an allowlist (`*` then `!src`, `!alembic`, `!alembic.ini`, `!pyproject.toml`,
`!uv.lock`, `!README*`), so `captures/`, `keys/`, `.env.dev`, `related-repos/` and `.git` cannot
arrive by someone forgetting to list them.

Rejected: **Wolfi / Chainguard `python`**, which §10 names and which ships no shell. Its free tier
publishes `latest` only, so the interpreter would move to 3.14 under a lock resolved for 3.13 on an
unrelated rebuild. The chosen base keeps a shell; the compensating controls are the compose
deployment's read-only root, dropped capabilities and `no-new-privileges`. Recorded as a deviation
from §10's "no shell utilities" in DESIGN.md rather than silently.

*(Changed during implementation, by operator decision: slim → Alpine.)* Measured on the same day
with the same pinned trivy: `python:3.13-slim-trixie` produced a 307 MB image with 56 HIGH/CRITICAL
findings (53/3) in Debian packages; `python:3.13-alpine` (3.24.1) a 200 MB image with 7 HIGH, all
`libuuid`, and no CRITICAL. Every native dependency in `uv.lock` (asyncpg, pynacl, cryptography,
cffi, pydantic-core, greenlet, sqlalchemy, markupsafe, websockets) ships a `musllinux` cp313 wheel,
so the Dockerfile changed only its base line. Consequences, each handled:

- **The test suite runs on glibc and the image on musl.** `build.sh` gains a `replay` gate after
  `smoke`: every committed capture is replayed on the host and inside the image (as UID 52037, read-only,
  no network) and the rendered output must be byte-identical.
- **musl's resolver** was checked against compose's service name: the former `migrate` service (`db upgrade`) and `web user add`
  reach `postgres` by name.
- **Nothing is deleted from the base.** The first Alpine build removed `pip` and `apk` in the final
  stage. That saves no bytes — the files stay in the base layers — and it hides them from the scan
  while still shipping them; removing `/lib/apk/db` as well made the scan report *zero* OS findings,
  a blind scan reading as a clean one. The final stage now only copies `/app` from the build stage,
  where alembic is byte-compiled and `chmod -R a+rX` applied; it runs no command.
  `tests/test_deployment_files.py` refuses a final stage with a `RUN` or an `rm`. The base's
  `pip`/`apk` remain, reported by the scan, and cannot install anything under the read-only root and
  non-root user.
- A future dependency without a musl wheel fails the build rather than needing a compiler, the
  signal this image already promises.

### D14. Compose: two services, two networks, required variables fail loudly

*(Changed during implementation, by operator decision.)* The first version had a third `migrate`
service under `profiles: [tools]` and delivered secrets as compose `secrets:` files under
`./secrets/`. The operator judged that too complicated: the deployment is now two services, the
platform migrates on start, and secrets come from the gitignored `.env` compose already reads.

- `sighop`: the image; `user: "${UID:?…}:${GID:?…}"`; `group_add: ["${DIALOUT_GID:?…}"]` (numeric,
  because the name `dialout` resolves against the *image's* `/etc/group`); `devices:` long syntax
  from `${SIGHOP_MODEM:?…}` (a `/dev/serial/by-id/…` path; by-id names contain colons) to
  `/dev/modem`; `read_only: true`; `tmpfs: [/tmp]`; `cap_drop: [ALL]`;
  `security_opt: [no-new-privileges:true]`; `init: true`; `restart: unless-stopped`;
  `stop_grace_period: 20s` (web grace 5 s plus writer flush and modem close);
  `ports: ["127.0.0.1:${SIGHOP_WEB_PORT:-8080}:8080"]`; `environment:` `DATABASE_URL` built from
  `${POSTGRES_PASSWORD:?…}` and `SIGHOP_SECRET_KEY` from `${SIGHOP_SECRET_KEY:?…}`; `command:` a list
  starting `run --migrate --device /dev/modem --web --web-host 0.0.0.0 --web-port 8080
  --web-allowed-host localhost:8080 --web-allowed-host 127.0.0.1:8080`, receive-only because §2
  makes that the default on a fresh install; `depends_on: postgres: condition: service_healthy`;
  `logging` with `json-file` `max-size`/`max-file` so a busy mesh's JSON events cannot fill the
  disk — the same concern §6 applies to `packet_log`.
- `postgres`: pinned `postgres:17-trixie` by digest, `POSTGRES_PASSWORD` from `.env`, named volume,
  `pg_isready` healthcheck, **only** on the `db` network, no `ports`.

**Migration on start.** `run --migrate` applies outstanding migrations before the schema-version
check and emits `database_migrated` with the revision before and after. It moves only a database
that is *behind*: one ahead of the image (an older image restarted after a newer one migrated) is
left untouched and refused by the check, as outside a container. A plain `run` still refuses a
behind database — the flag is how the deployment opts in, because in compose starting the new
image *is* the deploy. §6's objections (rollback meets an unknown schema; two instances racing
DDL) are answered by the ahead-refusal and by the deployment being one `sighop` container.

**Secrets from `.env`.** `DATABASE_URL` is interpolated from `POSTGRES_PASSWORD`, so the password
is written once and must be URL-safe (`openssl rand -hex 24`). Both values are `${VAR:?…}`: compose
refuses to start naming the unset one. The trade-off accepted: they are visible in
`docker inspect` to anyone in the `docker` group, which is root-equivalent anyway. D12's `_FILE`
variables were removed with the secret files.

Networks: `db` with `internal: true` (sighop and postgres); `web` ordinary bridge (sighop only),
which exists so the published port has a route. `${VAR:?message}` is compose's own "refuse and
name it", which is what `compose-deployment` asks for without a wrapper script.

No healthcheck on `sighop`: the image has no HTTP client, every panel route requires a session, and
an unauthenticated `/healthz` would be the first public route that reflects platform state. A run
that hits a fatal error exits, and `restart` handles that. Rejected: a Python one-liner healthcheck
against `/login` — it proves the web server answers, not that the radio is alive, and would give a
false green for the failure that matters.

Operating it: accounts are added with
`docker compose run --rm -it sighop web user add <name>` — an interactive prompt through the same
image, so the password never touches the compose file, the environment or shell history. On a
fresh database, `docker compose up -d` comes first: sighop migrates, then refuses to serve the
panel with no enabled account and restarts until one is added.

### D15. `build.sh`: gates as functions, scanner by image tarball

Bash with `set -euo pipefail`, a `gate <name> <command…>` helper that prints the gate, runs it, and
on failure prints `build failed at gate: <name>` and exits non-zero. Order: `lock` (`uv lock
--check`), `lint` (`uv run --locked ruff check`), `types` (`uv run --locked mypy`), `test` (`uv
run --locked pytest -q`; database tests skip without a URL, as they always have — the build does not
require Postgres), `image` (`docker build` with version from `uv version --short` and commit from
`git rev-parse --short=12 HEAD`, suffixed `-dirty` when `git status --porcelain` is non-empty),
`smoke`, `replay` (added with the Alpine base, D13: every committed capture renders byte-identically
on the host and in the image), `scan`.

`smoke`: `docker run --rm --read-only --tmpfs /tmp --user 52037:52037 --cap-drop ALL
--security-opt no-new-privileges <image> --help` and the same with `run --help` — `cli.py` imports
`web.app` at module level, so `--help` loads FastAPI, Jinja, PyNaCl and SQLAlchemy and proves the
whole application imports as a stranger on a read-only root.

`scan`: `docker save` the image to a temporary tarball and run a pinned `aquasec/trivy:<version>`
container against it with `--input`, the tarball and `.trivyignore` mounted read-only, a named
cache volume for the vulnerability database, `--severity HIGH,CRITICAL --exit-code 0
--show-suppressed`: every high and critical finding, fixed or not, is **printed and does not
fail the build**. The gate fails only when trivy cannot scan. Before scanning, the script refuses
a `.trivyignore` entry not immediately preceded by a `#` comment — the "states the reason" rule,
enforced in ten lines rather than by review.

*(Changed during implementation, by operator decision.)* The scan first failed the build on
fixable findings. On the day it was built, the newest digest of `python:3.13-slim-trixie` (the base
at the time)
carried 12 fixable HIGH/CRITICAL Debian package findings (gzip, pcre2, sqlite3, perl-base) with
nothing in sighop's own dependencies, so no image could be built at all. Distroless
(`gcr.io/distroless/python3-debian13`) was measured as an alternative: no shell and 3 of those 4
packages gone, but 6 fixable HIGH findings of its own that only Google's rebuild can clear, and
an image config that sets `User=0`. The operator kept the slim base and made the scan a report.

**CI** (`.github/workflows/build.yml`) runs `./build.sh` on pull requests and pushes. On a push to
the main branch or a manual run it pushes the verified image to GHCR as
`<commit12>-<YYYYMMDD>-<HHMMSS>` (UTC), posts tag and digest through the
`Sigurs/container-rebuilds` `notify-discord` action (`DISCORD_WEBHOOK` secret), and runs
`actions/delete-package-versions` with `min-versions-to-keep: 3`. `build.sh` builds with
`--provenance=false --sbom=false` so a push is one manifest — one package version — and "three
versions" means three tags rather than two tags and their attestations. Every action is pinned to
a commit. `build.sh` itself still never pushes.

Rejected: mounting `/var/run/docker.sock` into the scanner, which hands a third-party image
root-equivalent control of the host to read one image. Rejected: `grype` — equivalent for this
purpose; trivy is chosen for its ignore-file comments and `--show-suppressed`, which map directly
onto the spec.

`IMAGE` (default `sighop:<version>-<commit>`) is overridable from the environment — CI sets it to the
registry reference — and the script never pushes.

### D16. Tests keep running with no database and no Docker

- `web/auth.py`'s account lookup is a read-only-property `Protocol` (`AccountStore`), the pattern
  `RoomStorage` and the bot host already use; `tests/webfixtures.py` gains an in-memory store and a
  `signed_in(client, username)` helper that calls the **production** `SessionStore.issue()` and sets
  the cookie. There is no `auth=None` on `create_app`: it requires an authenticator, and the
  existing web tests move to the signed-in fixture.
- One test module signs in through the real `POST /login` with real Argon2id, so the full path is
  exercised at least once without making every test pay 64 MiB and tens of milliseconds.
- Throttle and session expiry take an injected clock, as the airtime and dedup tests already do.
- `WebUserRepository` tests are `@pytest.mark.database`, skipping without a URL.
- Container, compose and `build.sh` properties are checked by a `tests/test_deployment_files.py`
  that parses `Dockerfile`, `compose.yaml` and `.dockerignore` as text/YAML and asserts the hardening
  keys are present — cheap regression protection for a line someone deletes. Whether they *work* is
  the live exercise's job, because that needs Docker and the board.

## Risks / Trade-offs

- **A V4 reset may strand the container's device node.** The V4's native USB disappears from the
  bus on reset (milestone 2). Compose `devices:` creates `/dev/modem` once, by major:minor, at
  container start; if the board re-enumerates as `ttyACM1`, the by-id symlink on the host follows it
  but the container's node does not, and the reconnect loop retries a dead node forever. →
  The live exercise resets the board under the container and records what happens. If it strands,
  the mitigation is a `device_cgroup_rules` entry for the ACM major plus a bind mount of
  `/dev/serial/by-id`, decided on the measurement rather than in advance.
- **Plain HTTP exposes credentials on a non-loopback bind.** → Loopback default, compose publishes
  on host loopback, unsuppressible startup warning naming the tunnel remedy. Accepted by operator
  decision.
- **`--web` now needs Postgres (BREAKING).** A developer running the panel against a replay with no
  database loses that. → Milestone 8 already found a replay run cannot be administered; the panel is
  exercisable standalone against the dev database via `create_app` and a signed-in fixture, which is
  what write-parity used.
- **The Alpine base carries busybox's shell.** → Read-only root, all capabilities dropped,
  `no-new-privileges`, arbitrary UID; recorded as a §10 deviation with the reason (D13).
- **Sessions die on restart and on `docker compose up` recreating the container.** → By design
  (D3); the absolute lifetime would end them within a day anyway.
- **An open chat tab never idles out.** → Absolute 24 h lifetime is the backstop (D3).
- **Trivy's database download needs network at build time, and findings change daily.** → The
  scan reports rather than enforces (operator decision), so a newly published CVE shows in the
  build log without stopping a build. The cost is that nothing forces a base-image bump: someone
  has to read the report. A scan that cannot run still fails the gate.
- **CI publishes and prunes on every push to the main branch.** → Only the newest three versions
  survive; a deployment pinned to an older tag loses its image. Pin by digest in the Discord
  message, or re-run the workflow, to recover one.
- **Four concurrent Argon2id operations can hold 256 MiB.** → Two semaphores of two; the throttle
  refuses before verifying once an attacker has failed five times.
- **Revalidation adds a database read per session per minute on an otherwise read-free page
  load.** → Bounded by the existing statement timeout; a degraded database answers fast
  (`degraded` state) rather than waiting.

## Migration Plan

1. `sighop db upgrade` applies `0005`; the compose deployment's `run --migrate` does it on start.
2. `sighop web user add <name>` (or the `docker compose run --rm -it sighop web user add` form)
   before the first `run --web` on this build; without it `--web` refuses to start, naming this
   command.
3. Container deployment from scratch: `./build.sh` → write `.env` with `UID`, `GID`, `DIALOUT_GID`,
   `SIGHOP_MODEM`, `POSTGRES_PASSWORD`, `SIGHOP_SECRET_KEY` → `docker compose up -d` (migrates) →
   `docker compose run --rm -it sighop web user add <name>`.
4. Existing stored identities need nothing: `SIGHOP_SECRET_KEY` moves from `.env.dev` into `.env`
   with the same value.

Rollback: stop the run, downgrade to `0004` with `uv run alembic downgrade 0004` from a checkout
(there is no `sighop db downgrade`, and this change does not add one — §6 keeps schema changes
deliberate, and a downgrade that deletes accounts is the most deliberate of them), then run the
previous build, which refuses a database at `0005` and serves the milestone 8 unauthenticated
panel once downgraded, loopback by default.

Exit criterion (the live exercise, a `runbook.md` written in this change): the image built by
`./build.sh`, started by `docker compose up` as a non-root UID with the Heltec V4 mapped by
`/dev/serial/by-id`; an operator signs in from a browser on the host; enables transmit with a
password re-entry; sends a direct message as a `dev-` identity that the stock peer acknowledges;
`docker compose restart sighop` signs them out and the conversation is still there after signing
back in; and every `web_request` and `web_guarded_action` in the container's log names the account.
The board is reset once while containerised and the outcome recorded, whichever it is.

## Open Questions

- **Does a V4 reset strand `/dev/modem` inside the container?** Answered by the live exercise;
  changes only compose device lines, not specs or tasks.
- **Is an `arm64` image wanted** (a Raspberry Pi beside the aerial is the obvious deployment)? All
  native dependencies publish aarch64 wheels; adding `--platform` to `build.sh` later changes no
  spec.
- **Are 12 h idle / 24 h absolute right for how the panel is actually left open?** Constants in one
  module; revisit after the exercise.
