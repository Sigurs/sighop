## Why

The compose deployment bundles its own Postgres, but development already runs against the
shared external dev database, which needed a second override file (`compose.dev.yaml`) that
resets half of `compose.yaml`. Production will also use an external database, so the bundled
service serves neither environment, and the two files make "run the same compose in prod"
harder than it should be. Collapsing to one compose file whose per-host differences live
entirely in `./.env` makes dev and prod the same command.

## What Changes

- **BREAKING** `compose.yaml` drops the `postgres` service, the `pgdata` volume and the
  internal `db` network. The deployment is one service: `sighop`.
- **BREAKING** `sighop` takes `DATABASE_URL` from `./.env` (required, compose refuses to start
  without it) instead of building it from `POSTGRES_PASSWORD`; `POSTGRES_PASSWORD` is no
  longer read. The `depends_on` on a healthy database goes.
- `compose.dev.yaml` is deleted. Dev and prod run the same `docker compose up -d`; each host's
  gitignored `./.env` supplies `UID`, `GID`, `DIALOUT_GID`, `SIGHOP_MODEM`, `DATABASE_URL`,
  `SIGHOP_SECRET_KEY`, and optionally `SIGHOP_IMAGE`.
- The web panel's publication becomes env-configurable with today's behaviour as default:
  `SIGHOP_WEB_BIND` (published host address, default `127.0.0.1`), `SIGHOP_WEB_PORT` (already
  exists, default `8080`) and `SIGHOP_WEB_ALLOWED_HOST` (one extra host name the panel answers
  to, e.g. a reverse-proxy name). The built-in allowed hosts follow the published port instead
  of hardcoding `8080`.
- `sighop` stays on a routed network so it can reach an off-host database.
- `.env.example`, the compose file's header, DESIGN.md §10/§11 and
  `tests/test_deployment_files.py` are updated to match.

## Capabilities

### New Capabilities

_None._

### Modified Capabilities

- `compose-deployment`: the deployment becomes a single platform service against an external
  database named by `DATABASE_URL`; the "database reachable only by the platform" requirement
  is removed; secret delivery covers `DATABASE_URL` instead of the database password; the
  web-publication requirement gains env-configured address, port and extra allowed host with
  loopback defaults; the migrations requirement no longer says "platform and database services".

## Impact

- Files: `compose.yaml` (rewrite), `compose.dev.yaml` (delete), `.env.example`, `DESIGN.md`
  §10 and §11 layout, `tests/test_deployment_files.py`.
- No application code changes: `run --migrate`, `DATABASE_URL` validation, and
  `--web-allowed-host` (which already dedupes repeated values) are used as they are.
- Operators: existing `./.env` files must add `DATABASE_URL` and may drop `POSTGRES_PASSWORD`;
  a dev host using `COMPOSE_FILE=compose.yaml:compose.dev.yaml` must remove that line. Any
  data in a local `pgdata` volume is no longer used by the stack (the dev host's volume was
  already removed during `web-first-run-setup`'s live check).
- The database must be reachable from the container's bridge network and at a revision no
  newer than the image (unchanged `--migrate` rules).
