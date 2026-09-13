## 1. Schema and migration `0005` (`web-auth`)

- [x] 1.1 Add the `web_user` model — `id`, `username` (lower-cased, `UNIQUE`), `password_hash`, `enabled`, `created_at`, `password_set_at`, both timestamps `TIMESTAMPTZ` (design D1); verify a unit test asserts the unique constraint and the timestamp types
- [x] 1.2 Write migration `0005` creating the table, with a `downgrade()` that drops it and a docstring stating that a downgrade deletes every account and that `run --web` then cannot start; verify an upgrade→downgrade→upgrade cycle in a throwaway schema leaves the schema at head with no leftover objects
- [x] 1.3 Verify by test that the schema-version check refuses a database at `0004` when the code expects `0005`, naming both revisions and the reconciling command
- [x] 1.4 Add username normalisation (NFKC, `casefold`, refuse empty/whitespace/control characters, 64-character cap) as one function used by the repository, the CLI and sign-in; verify table-driven tests including a mixed-case and a compatibility-form username normalising to the same value

## 2. The account repository (`web-auth`)

- [x] 2.1 Add `WebUserRepository` beside the others — `add`, `get`, `list`, `set_password` (updates `password_set_at`), `set_enabled`, `remove`, `count_enabled` — each returning the project's `Outcome` type and refusing a duplicate normalised username by naming the existing row; verify `@pytest.mark.database` tests for each operation, including the case-only duplicate
- [x] 2.2 Expose the repository through `Persistence` and define the read-only-property `AccountStore` `Protocol` in `web/auth.py` that it satisfies structurally (design D16); verify a mypy-checked assertion that the repository satisfies the protocol and an in-memory store in `tests/webfixtures.py` does too
- [x] 2.3 Verify by test that no `repr`, `as_json` or listing output of an account carries its `password_hash`

## 3. Secrets from files (`runtime-cli`) — removed

Built (3.1–3.3: `DATABASE_URL_FILE`/`SIGHOP_SECRET_KEY_FILE`, both-set refusal, `.env.example`) and then
removed by operator decision once compose took secrets from `.env` (12.4) and nothing used them. What
survived: `keys import`/`export` read the secret through `Config`, with a static test forbidding
`os.environ` reads around it.

- [x] 3.4 Remove the `_FILE` companions from `config.py`, their tests, messages and spec scenarios; verify the suite passes and `grep -r _FILE src` finds no secret companion

## 4. Account commands (`runtime-cli`)

- [x] 4.1 Add the `sighop web user` noun with `add`, `list`, `passwd`, `disable`, `enable`, `remove`, each requiring a configured database and failing with a statement that accounts are stored there when none is; verify `tests/test_web_user_cli.py` covers each verb's success output **and** its no-database refusal — one test per verb, not one for the noun
- [x] 4.2 Read passwords through the room commands' existing prompt/stdin path, asking twice on a TTY and refusing a mismatch, and refusing a password-looking positional argument; verify tests for stdin input, a mismatched double entry and an argv password
- [x] 4.3 State each command's consequence: `add` that the account can sign in to any run on this database, `passwd` and `disable`/`remove` that existing sessions end within a minute; verify tests assert each sentence
- [x] 4.4 Refuse disabling or removing the last enabled account without `--allow-no-accounts`, saying no run could then start its web interface; verify tests for the refusal and for the acknowledged path
- [x] 4.5 Make `list` show username, enabled state, created and password-set times and no hash; verify a test asserts the columns and the absence of `$argon2id$` in the output

## 5. Sessions, throttle and sign-in core (`web-auth`)

- [x] 5.1 Implement `SessionStore` in `web/auth.py` — keyed by `sha256(token)`, `MAX_SESSIONS = 256` evicting oldest, 12 h idle, 24 h absolute, per-session `csrf_token`, `issue()` / `resolve()` / `end()` with an injected clock (design D3); verify tests for idle expiry, absolute expiry under continuous activity, eviction at the bound, and that the store's `repr` and contents never contain a raw token
- [x] 5.2 Implement revalidation at most every 60 s against the `AccountStore`: end the session on a missing row, a disabled account or a changed `password_set_at`; keep it but mark it unverified when the store reports degraded (design D4); verify tests for each ending reason, for the one-read-per-minute bound, and for the degraded case keeping the session
- [x] 5.3 Implement `LoginThrottle` — two LRU maps of 4096 keys (normalised username, client address), five free failures then `min(2 ** (n - 5), 900)` s, cleared on success, injected clock (design D8); verify tests for per-username and per-address throttling, reset on success, unknown usernames throttled identically, and memory bounded after 10 000 distinct keys
- [x] 5.4 Implement the sign-in decision: throttle check first (refusing without verifying), then verify against the row's hash or a startup-computed `DUMMY_HASH`, disabled accounts verifying and then failing; give the panel its own `PasswordHasher` instance, separate from the room server's; verify tests with a counting hasher that an unknown user, a disabled user and a wrong password each run exactly one verification and a throttled attempt runs none
- [x] 5.5 Emit `web_login` with outcome and reason (`unknown_user`, `disabled`, `bad_password`, `throttled`, `success`) and `web_session_ended` with reason (`logout`, `idle`, `lifetime`, `account_changed`, `evicted`); verify tests capture both events and assert no field contains the submitted password

## 6. The guard enforces authentication (`web-auth`, `web-server`)

- [x] 6.1 Extend `RequestGuard`: host check → session from `sighop_session` cookie → public-path check (`GET`/`POST /login`, `/static/`) → provenance → handler; unauthenticated safe methods get `303` to `/login?next=…` with `next` accepted only as a same-origin absolute path, unsafe methods get `401` (design D5); verify tests for each branch, including an off-site `next` value being ignored
- [x] 6.2 Make the provenance token per-session for signed-in requests and per-process for the login form, refusing another session's token like a missing one (design D6); verify tests for a cross-session token and for a login POST without the process token being refused before any verification runs
- [x] 6.3 Add `actor` to every `web_request` event — the username, or `unauthenticated`; verify a test asserts both values across a signed-in and an anonymous request
- [x] 6.4 Authenticate the feed WebSocket: session required and `Origin` matched against allowed hosts, closing with `1008` before `accept()`, and the connection's closing event carrying `actor`; verify tests for no cookie, a foreign `Origin`, and a signed-in connection receiving records
- [x] 6.5 Walk `app.routes` with the existing `registered_routes` helper and assert every route outside the public set refuses a request with no session; verify the test fails when a deliberately unprotected route is added to a test app and passes without it
- [x] 6.6 Verify by test that every file under `web/static/` is served without a session and contains no template syntax, token, or key material

## 7. Sign-in and sign-out routes (`web-auth`)

- [x] 7.1 Add `routes/session.py` and `templates/login.html`: `GET /login` renders the form with the process token; `POST /login` runs the decision from 5.4, rotates any presented session, sets the cookie `HttpOnly; SameSite=Strict; Path=/` with no `Secure` and no expiry (design D9), and redirects to `next` or `/`; verify tests assert the cookie attributes exactly and that the pre-login cookie value grants nothing afterwards
- [x] 7.2 Return the same status and body for every failed sign-in; verify a test compares the responses for an unknown user, a disabled user and a wrong password byte for byte
- [x] 7.3 Add `POST /logout` (session-token guarded) ending the session and clearing the cookie, and a sign-out control in `base.html` showing the signed-in username; verify tests that logout ends the session, that `GET /logout` does not exist, and that the header names the user
- [x] 7.4 Sign in through the real `POST /login` with real Argon2id in one test module (design D16); verify it passes with no database configured, using the in-memory account store

## 8. Re-authenticated guarded actions (`web-auth`, `web-admin`)

- [x] 8.1 Make `audit()`'s `actor` a required keyword with no default and pass the session's username at every call site; verify mypy fails on a call without it and a test asserts `actor` on each of the five guarded actions' events
- [x] 8.2 Add a password field to the confirmation forms for reveal key, export key, enable transmit and raise ceiling, and require a fresh `AccountStore` read plus a successful verify after the nonce is spent (design D7); verify tests for each of the four actions: right password succeeds, wrong password refuses with the action's own event, missing field refuses identically
- [x] 8.3 Feed a guarded-action password failure to the throttle under that username without ending the session; verify a test that five wrong re-authentications throttle the next sign-in attempt for that user while the session still loads pages
- [x] 8.4 Refuse every guarded action for an unverified session (database degraded at revalidation) with "this account cannot currently be verified"; verify a test with a degraded account store
- [x] 8.5 Leave `post_to_room` confirm-and-nonce with no password field, now carrying `actor`; verify a test asserts the post succeeds without a password and its event names the user
- [x] 8.6 Render guarded actions in the run's output with the account that made them; verify a test asserts the transmit-enabled and ceiling-raised lines name the account

## 9. Startup, binding and allowed hosts (`web-server`, `runtime-cli`)

- [x] 9.1 In `_attach_web`, before binding, refuse `--web` with no database configured and with zero enabled accounts, each naming its fix (`DATABASE_URL`/`--database-url`; `sighop web user add`); verify tests assert both refusals happen with no socket bound and nothing received
- [x] 9.2 Add repeatable `--web-allowed-host`, validated (no `*`, empty, scheme or path) and unioned with the bind-derived set, bare names added with and without the bound port (design D10); verify tests that `0.0.0.0` plus `localhost:8080` serves `Host: localhost:8080`, refuses an unnamed host with `421`, and that `*` fails startup with no port bound
- [x] 9.3 Rewrite `startup_lines()` and `report()`: loopback line states sign-in required and the enabled account count; non-loopback adds the unsuppressible plain-HTTP/unencrypted-credentials/tunnel warning; the event carries `authenticated=true`, `encrypted=false`, `accounts_enabled`; remove `NO_AUTHENTICATION` (design D11); verify tests assert both line sets, the event fields, and that no option removes the warning
- [x] 9.4 Verify by test that a forwarding header (`X-Forwarded-For`, `X-Forwarded-Proto`, `Forwarded`) changes neither the throttle key, the request event's client, nor the cookie attributes
- [x] 9.5 Make `create_app` require an authenticator (no `None` default) and move `tests/webfixtures.py` to a `signed_in(client, username)` helper built on the production `SessionStore.issue()`; verify the whole existing web suite passes on the signed-in fixture and `grep` finds no auth-bypass parameter in `src/sighop/web/`

## 10. Interface wording (`web-admin`, `web-chat`)

- [x] 10.1 Add account management to the absent-capabilities statement, naming `sighop web user` and the reason (design D2); verify a test asserts the command and the reason are on the page
- [x] 10.2 Remove the no-database branches from chat and the panel's persistence views that can no longer be reached, keeping the degraded-database wording, and add the "stored history cannot be read" state for a conversation opened while degraded; verify `web-chat` tests for mid-conversation degradation and for opening a conversation while degraded
- [x] 10.3 Verify by test that the corpus replay's delivered, duplicate, considered, contact and path counts are byte-identical with the authenticated interface wired in and without it

## 11. Container image (`container-image`)

- [x] 11.1 Write `.dockerignore` as an allowlist (`*`, then `!src`, `!alembic`, `!alembic.ini`, `!pyproject.toml`, `!uv.lock`) (design D13); verify a built image contains no `tests/`, `captures/`, `keys/`, `.env*`, `.git` or `related-repos/` by listing its filesystem
- [x] 11.2 Write the two-stage `Dockerfile` on a digest-pinned `python:3.13-slim-trixie` (since switched to `python:3.13-alpine`, 13.8), `uv` copied from a pinned image, `uv sync --locked --no-dev --no-install-project` then `--no-editable`, bytecode compiled at build; verify `docker build` succeeds and fails when `uv.lock` is made stale
- [x] 11.3 Final stage: copy `/app` (venv, `alembic/`, `alembic.ini`, byte-compiled and `chmod -R a+rX` in the build stage) with no `RUN`, set `SIGHOP_ALEMBIC_DIR`, `PATH`, `PYTHONDONTWRITEBYTECODE`, `PYTHONUNBUFFERED`, no `USER`, `ENTRYPOINT ["sighop"]`, `CMD ["--help"]`, OCI labels and `SIGHOP_COMMIT_HASH` from build arguments; verify `docker inspect` shows no user and the labels, and that the image adds no `gcc`, `cc` or `uv` binary (the base's `pip`/`apk` are left in place, not hidden)
- [x] 11.4 Verify the image runs `--help` as `--user 52037:52037 --read-only --tmpfs /tmp --cap-drop ALL`, that a structured event it emits carries the build's version and commit, and that `docker history --no-trunc` and the layers contain no secret or keyfile
- [x] 11.5 Verify `sighop db current` from the image alone reports revisions against the development database, with no checkout mounted

## 12. Compose deployment (`compose-deployment`)

- [x] 12.1 Write `compose.yaml` with `sighop` and `postgres` only (a `migrate` service was removed by operator decision, 12.6), the `db` network `internal: true` and a `web` bridge, per design D14; verify `docker compose config` resolves with the example environment and fails naming each of `UID`, `GID`, `DIALOUT_GID`, `SIGHOP_MODEM`, `POSTGRES_PASSWORD`, `SIGHOP_SECRET_KEY` when unset
- [x] 12.2 Harden `sighop`: `user`, numeric `group_add`, `devices` to `/dev/modem`, `read_only`, `tmpfs: [/tmp]`, `cap_drop: [ALL]`, `no-new-privileges`, `init: true`, `restart: unless-stopped`, `stop_grace_period: 20s`, `json-file` log rotation; verify the resolved `docker compose config` carries each key
- [x] 12.3 Postgres on the internal network only, no `ports`, named volume, `pg_isready` healthcheck, `POSTGRES_PASSWORD` from `.env`; sighop `depends_on` it healthy; publish the panel on `127.0.0.1` only with `--web-allowed-host localhost:8080` and `127.0.0.1:8080`; verify `docker compose ps` shows no host port for postgres and `ss -ltn` shows the panel on 127.0.0.1 only
- [x] 12.4 Secrets from the gitignored `.env` (operator decision, replacing compose `secrets:` files): `POSTGRES_PASSWORD` for postgres and interpolated into sighop's `DATABASE_URL`, `SIGHOP_SECRET_KEY` for sighop; verify `.env` is ignored, that compose refuses to start naming an unset one, and that no secret value appears in any committed file
- [x] 12.5 Add `tests/test_deployment_files.py` parsing `Dockerfile`, `.dockerignore` and `compose.yaml` and asserting the hardening keys from 11.x and 12.x are present (design D16); verify the test fails when `read_only` or `cap_drop` is deleted from `compose.yaml`
- [x] 12.6 Add `run --migrate` applying outstanding migrations before the schema-version check (a database ahead of the code still refused, `database_migrated` event with before/after) and start compose's `sighop` with it, removing the `migrate` service (operator decision); verify a database test that an empty schema reaches head and a second start applies nothing, a test that `--migrate` without a database fails naming `DATABASE_URL`, and `tests/test_deployment_files.py` asserting two services and `--migrate`

## 13. Build script (`build-script`)

- [x] 13.1 Write `build.sh` with the `gate` helper and gates `lock`, `lint`, `types`, `test`, `image`, `smoke`, `scan`, stopping at the first failure and naming it (design D15); verify by introducing a lint violation that the script exits non-zero naming `lint` and builds no image
- [x] 13.2 Compute version from `uv version --short` and commit from `git rev-parse --short=12 HEAD` with `-dirty` on a modified tree, and report the image reference, version and commit on success; verify both a clean and a dirty build's reported commit
- [x] 13.3 Implement `smoke` as two constrained `docker run`s (`--help`, `run --help`) as UID 52037 on a read-only root with capabilities dropped; verify the gate fails against a test image that writes to its root at start-up
- [x] 13.4 Implement `scan` by `docker save` tarball into a pinned `aquasec/trivy` container (no Docker socket mounted): one reporting pass with `--severity HIGH,CRITICAL --exit-code 0 --show-suppressed` that prints fixed and unfixed findings and fails only when the scan cannot run (operator decision, replacing the failing pass); verify the scan runs on a host with no trivy installed
- [x] 13.5 Refuse a `.trivyignore` entry not immediately preceded by a `#` reason comment, and commit an empty `.trivyignore` explaining the rule; verify the script fails on an uncommented entry and passes with a commented one
- [ ] 13.6 Run `./build.sh` end to end on a clean tree and verify it exits 0, reporting the image reference
- [x] 13.8 Switch the base to digest-pinned `python:3.13-alpine`, delete nothing the base ships (deleting in a later layer saves no bytes and hides files from the scan), and add a `replay` gate after `smoke` comparing every committed capture's output on the host and in the image byte for byte (operator decision, design D13); verify a full `./build.sh` exits 0 with the gate reporting all captures identical, the scan reporting Alpine packages, and compose's `db upgrade` resolving `postgres` by name
- [x] 13.7 Add `.github/workflows/build.yml`: `./build.sh` on pull requests and pushes; on a push to the main branch or a manual run, push to GHCR as `<commit12>-<YYYYMMDD>-<HHMMSS>`, notify through `Sigurs/container-rebuilds`' `notify-discord` action, and keep only the newest three package versions; build with `--provenance=false --sbom=false` so a push is one version; pin every action to a commit; verify by `tests/test_deployment_files.py` and a single-manifest local build

## 14. DESIGN.md

- [x] 14.1 §8: replace the milestone 8 authentication correction with authentication as built — accounts in the database, terminal-only management, in-memory sessions, re-authenticated guarded actions, `actor` — and correct "secure cookie" to `HttpOnly`/`SameSite=Strict` over plain HTTP with the reason (design D9); update "What the interface deliberately does not expose" to add accounts and remove "this build's port has no authentication" as the migration rationale; verify by review
- [x] 14.2 §6: add `web_user` (migration `0005`) to the built tables with the downgrade consequence; verify by review
- [x] 14.3 §10: record the container, compose and build script as built, including the slim-base deviation from "no shell utilities" and why Wolfi was rejected (design D13), the numeric `DIALOUT_GID`, secrets from `.env`, migration on start, and the absent healthcheck with its reason; verify by review
- [x] 14.4 §11: add `web/auth.py`, `routes/session.py`, `.dockerignore` and `.trivyignore` to the layout; verify by review

## 15. Live exercise

- [x] 15.1 Write `runbook.md` in this change for the exit criterion in design.md's Migration Plan, using existing `dev-` identities and the stock peer; verify by review before any transmission
- [ ] 15.2 Run the exercise: `./build.sh`, compose up as a non-root UID with the V4 by `/dev/serial/by-id`, sign in from a host browser, enable transmit with password re-entry, send an acknowledged DM, `docker compose restart sighop`, sign back in and find the conversation; verify the container log shows `actor` on every `web_request` and `web_guarded_action`
- [ ] 15.3 Reset the board once while containerised and record whether `/dev/modem` recovers; if it strands, apply the design's device-rule mitigation and re-verify
- [ ] 15.4 Record milestone 9's findings in DESIGN.md §12 and resolve or restate design.md's open questions; verify by review
