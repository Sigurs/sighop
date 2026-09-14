## Context

`compose.yaml` (milestone 9) runs `sighop` plus a digest-pinned `postgres:17-trixie` on an
internal `db` network, and builds `DATABASE_URL` from `POSTGRES_PASSWORD`. The uncommitted
`compose.dev.yaml` overrides it for the development host: `environment: !reset`, `env_file:
.env.dev`, `depends_on: !reset`, `networks: !override [web]`, a `local-db` profile to keep
postgres out, and `name: sighop-dev`. The dev database is off-host (`172.20.4.20:30432`).

Constraints carried over unchanged: the hardening block (user/group_add/devices, read-only root,
tmpfs, cap_drop, no-new-privileges, init, grace period, restart, log rotation), `run --migrate`,
`${VAR:?reason}` refusal for every required value, and `tests/test_deployment_files.py` reading
the compose file as text without a YAML library.

Application facts this design relies on (checked, not assumed): `DATABASE_URL` is validated at
startup by `config.py` (asyncpg driver only, libpq query parameters refused, password redacted);
`validate_allowed_hosts` in `web/app.py` accepts `name[:port]` and silently drops duplicates.

## Goals / Non-Goals

**Goals:**
- One compose file; `docker compose up -d` is the command on every host.
- Defaults reproduce today's loopback-only panel exactly.

**Non-Goals:**
- No application code change (no new env vars read by `sighop` itself).
- No TLS for the panel, no bundled reverse proxy.
- No database provisioning, backup or network restriction — the external database's host owns that.
- No more than one extra allowed host name.

## Decisions

### D1. Container environment by interpolation, not `env_file:`
`environment:` keeps `DATABASE_URL: "${DATABASE_URL:?set DATABASE_URL …}"` and
`SIGHOP_SECRET_KEY: "${SIGHOP_SECRET_KEY:?…}"`, both interpolated from `./.env`.
- Keeps compose's refuse-and-name behaviour for the two secrets (spec: "refuses to start naming the
  missing variable"). `env_file:` does not refuse on an unset key.
- `env_file: .env` would also copy `UID`, `GID`, `DIALOUT_GID`, `SIGHOP_MODEM`, `COMPOSE_*` into the
  container — noise at best.
- Alternative rejected: `env_file: ${SIGHOP_ENV_FILE:-.env}` so dev can point at `.env.dev` — a second
  indirection for one duplicated line.

### D2. `./.env` is the only file compose reads; `.env.dev` stays for host runs
Compose already auto-loads `./.env`. `.env.dev` keeps its role for `uv run --env-file .env.dev`.
On the dev host `DATABASE_URL` and `SIGHOP_SECRET_KEY` therefore appear in both files; `.env.example`
says so and notes that a superset `./.env` also works for `uv run --env-file .env`. Both files are
already gitignored.

### D3. Default network, no `networks:` key
With no database service the `db`/`web` split has nothing to separate. Dropping `networks:` gives
the project's default bridge, which routes out to the off-host database. Alternative: keep a named
`web` network — same behaviour, one more thing to read.

### D4. Web publication from three optional variables
```yaml
ports:
  - "${SIGHOP_WEB_BIND:-127.0.0.1}:${SIGHOP_WEB_PORT:-8080}:8080"
command:
  … --web-allowed-host localhost:${SIGHOP_WEB_PORT:-8080}
    --web-allowed-host 127.0.0.1:${SIGHOP_WEB_PORT:-8080}
    --web-allowed-host ${SIGHOP_WEB_ALLOWED_HOST:-localhost:${SIGHOP_WEB_PORT:-8080}}
```
- The Host header a browser sends carries the *published* port, so the built-in names follow
  `SIGHOP_WEB_PORT`; today's hardcoded `:8080` is wrong whenever that variable is set.
- Compose cannot omit an argument when a variable is unset, so the third flag always exists and
  defaults to a name already listed; `validate_allowed_hosts` dedupes it, making the default
  exactly the loopback pair (spec scenario "exactly the loopback ones").
- Nested default interpolation is supported by Compose v2 (host has 5.4.0); verified with
  `docker compose config` during implementation.
- Container-side bind stays `0.0.0.0:8080`; only host publication moves.
- Alternatives rejected: a comma-separated list (needs `sighop` to parse it — a code change);
  teaching `sighop run` to read `SIGHOP_WEB_ALLOWED_HOST` itself (widens the CLI's env surface for a
  deployment concern).

### D5. No `name:`; project name from the directory or `COMPOSE_PROJECT_NAME`
The file is identical on every host, so it carries no project name. The default (`sighop`, from the
checkout directory) matches the milestone-9 stack; a host that needs another sets
`COMPOSE_PROJECT_NAME` in `./.env`.

### D6. Image unchanged
`image: ${SIGHOP_IMAGE:-sighop:local}` already lets prod name a GHCR tag from CI.

### D7. Tests stay text-based
`test_deployment_files.py` changes: one service (`["sighop"]`); postgres test replaced by one asserting
no `postgres`, `pgdata`, `internal: true`, `depends_on` or `POSTGRES_PASSWORD` anywhere; `DATABASE_URL:
"${DATABASE_URL:?` present; ports and allowed-host assertions match D4; `compose.dev.yaml` absent.
Hardening and mutation tests unchanged.

## Risks / Trade-offs

- [Running dev stack is project `sighop-dev`; after the change the same checkout resolves to `sighop`,
  so a plain `up` would start a second container competing for `/dev/modem` while the old one still
  holds it] → Migration step 1 stops the old project by name before anything else.
- [A `$` in the database password inside `./.env` is interpolated by compose] → `.env.example` says to
  single-quote the value; passwords must already be URL-encoded for the URL.
- [Setting `SIGHOP_WEB_BIND` to a non-loopback address serves plain HTTP to the network] → same
  startup warning as today; `.env.example` says to front it with a TLS proxy and set
  `SIGHOP_WEB_ALLOWED_HOST` to the proxy's name.
- [IPv6 bind addresses need brackets in `ports`] → noted in `.env.example`.
- [Dev and prod use different databases but one secret variable each; pointing prod at the dev DB with
  the prod key fails to unseal identities] → unchanged behaviour; noted in `.env.example`.

## Migration Plan

1. Dev host: `docker compose -p sighop-dev down` (the override stack) — and `docker compose -f
   compose.yaml down` if the old bundled stack is up. Remove any `COMPOSE_FILE=` line from `./.env`.
2. Edit `./.env`: add `DATABASE_URL` and `SIGHOP_SECRET_KEY` (same values as `.env.dev`), delete
   `POSTGRES_PASSWORD`.
3. `docker compose config` to check interpolation, then `docker compose up -d`; confirm the startup
   event and panel sign-in at `http://localhost:8080`.
4. Prod host: same `./.env` keys with prod values and `SIGHOP_IMAGE` set to the GHCR tag.
5. Rollback: `git checkout` the previous `compose.yaml`/`compose.dev.yaml` and restore
   `POSTGRES_PASSWORD`/`COMPOSE_FILE` in `./.env`. Leftover `sighop_pgdata` volumes, if any, are not
   removed by this change.
