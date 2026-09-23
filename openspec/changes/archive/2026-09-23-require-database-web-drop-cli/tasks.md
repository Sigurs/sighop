# Tasks

Ordered so the suite stays runnable throughout: the test harness gains its database first, the
panel's tests are made self-standing before the command implementations they compare against are
deleted, and `cli.py` is removed only once nothing imports it.

**Ordering corrected during apply.** Group 2 makes `Config.database` non-optional and drops
`from_environment`'s `database_url` keyword, which `cli.py` calls in seven places — so the command
line stops importing the moment group 2 lands, and 194 tests in the six CLI test files fail until it
is deleted. Propping `cli.py` up in between would be work on code that group 7 deletes. The groups
are therefore done in the order **1, 2, 3, 4, 6, 7, 5, 8, 9, 10**: the boot path is extracted while
`cli.py` is still there to extract it from; group 4 comes straight after because `boot.py` cannot
type-check until `Runtime.persistence` is required; then the tests are rewritten, then the command
line goes, and the remaining groups run against a green suite again.

## 1. The test suite requires a database

The suite works in a schema of its own inside the database the environment names, and drops it when
the run ends. No container-started database: the development container has no Docker socket, so that
fallback would work on the host and fail in the container (design D6).

- [x] 1.1 Keep the dev dependency group free of a container-engine dependency, and replace the `database` marker in `pyproject.toml` with a comment stating why there is no longer a subset to select; verify `uv sync` resolves and no dependency was added
- [x] 1.2 Make `tests/dbfixtures.py`'s `database_url` fixture fail rather than skip when neither `SIGHOP_TEST_DATABASE_URL` nor `DATABASE_URL` is set, keeping the per-run `sighop_test_<random>` schema and its `CASCADE` drop; verify `uv run --env-file .env.dev pytest tests/test_db_engine.py` passes
- [x] 1.3 Refuse the run once in `pytest_configure` with a message naming both variables, rather than once per test; verify `pytest` with both variables unset exits with pytest's usage-error code and prints the message a single time
- [x] 1.4 Delete `SKIP_REASON`, `pytest_collection_modifyitems`, all 516 `@pytest.mark.database` decorators and the meta-test that policed the marker; verify `uv run --env-file .env.dev pytest --collect-only` reports no skips and no unknown-marker warnings
- [x] 1.5 Run the whole suite against the configured database and record the result; verify `uv run --env-file .env.dev pytest` passes

## 2. Configuration moves into `Config`

- [x] 2.1 Add a field to `Config` for each surviving `run` flag with the variable names in design D2, each parsed and validated in `from_environment`; verify new unit tests in `tests/test_config.py` cover every variable's default, accepted values and rejection message
- [x] 2.2 Make `Config.database` non-optional and drop `from_environment`'s `database_url` keyword; verify `uv run mypy src` passes and the callers that passed a URL are gone
- [x] 2.3 Implement `check_environment(config)` returning every problem found, ordered database URL, secret key, then the rest (design D3); verify a test asserting that three simultaneous mistakes are all reported in one run
- [x] 2.4 Make the missing-secret refusal print the `openssl rand -base64 32` line; verify a test asserting the exact command appears in the output and the exit code is non-zero
- [x] 2.5 Update `.env.example` to list every `SIGHOP_*` variable against the flag it replaces, and to drop the "leave it unset to run entirely in memory" paragraph; verify by reading it against the field list from 2.1

## 3. The boot path moves out of `cli.py`

- [x] 3.1 Create `src/sighop/boot.py` holding `open_persistence`, `migrate_on_start`, `_run_config`, `_attach_web`, `_live_startup`, `_webhook_secret` and `_web_sealing_secret`, taking `Config` rather than `argparse.Namespace`; verify `uv run mypy src` passes and the new module imports nothing from `cli`
- [x] 3.2 Write `boot.main() -> int`: refuse any argument, run `check_environment`, migrate, then build the `Runtime` and attach the web interface unconditionally; verify a test asserting that an argument exits non-zero and that a complete environment reaches `Runtime.run`
- [x] 3.3 Point `[project.scripts] sighop` at `sighop.boot:main`; verify `uv run sighop` with an empty environment prints the refusals and exits non-zero
- [x] 3.4 Make migration-on-start unconditional and delete the `--migrate` opt-in path; verify the existing migrate-on-start tests pass against `boot.main`

## 4. The optional database is removed from the runtime

- [x] 4.1 Change `Runtime.persistence` to `Persistence` and delete every `if self.persistence is None` branch in `src/sighop/runtime.py` (design D4); verify `uv run mypy src` passes and `grep -n "persistence is None" src/` returns nothing
- [x] 4.2 Delete `PERSISTENCE_OFF`, `WEBHOOKS_OFF_NO_DATABASE`, `BOTS_OFF`, `ROOMS_OFF` and `CHANNELS_OFF` from `src/sighop/monitor/render.py` and their call sites, keeping `WEBHOOKS_OFF_REPLAY`; verify `uv run pytest tests/test_render.py` passes
- [x] 4.3 Reduce `src/sighop/web/render.py`'s three-state persistence rendering to present-or-degraded and delete `NO_DATABASE`; verify `uv run pytest tests/test_web_display.py tests/test_web_dashboard.py` passes
- [x] 4.4 Delete the no-database branches from the panel's routes and templates (webhooks page, chat page, dashboard feed); verify `uv run pytest tests/test_web_admin.py tests/test_web_chat.py` passes

  *Done by tightening `PanelState.persistence` to `Persistence` (operator decision during apply), so
  the ~85 `page.persistence is None` branches across the routes are gone rather than unreachable.
  Page tests therefore carry a real database: `tests/dbfixtures.py` gains `fresh_persistence` (the
  run's schema, emptied, over an unpooled engine so a sync test client's loop leaves nothing behind)
  and `default_persistence`, which a module opts into with
  `pytestmark = pytest.mark.usefixtures("default_persistence")` and which `stub_state()` and the
  runtime-building helpers fall back to. Tightening surfaced one latent bug, fixed: choosing a
  default chat identity that had been removed meanwhile raised `UnknownIdentityError` as an error
  page instead of refusing.*
- [x] 4.5 Delete the tests that assert no-database behaviour across `tests/test_runtime_persistence.py`, `tests/test_web_admin.py`, `tests/test_web_chat.py`, `tests/test_web_dashboard.py` and `tests/test_durable_contacts.py`; verify the full suite passes and no test name mentions a run without a database

## 5. The web interface stops being optional

- [x] 5.1 Remove the opt-in from the boot path so the interface is always attached, and make a failure to bind a startup failure; verify a test asserting that a bind failure prevents the node from running the radio
- [x] 5.2 Read the bind address, port and allowed host names from `Config` and keep the non-loopback warning unsuppressible; verify `uv run pytest tests/test_web_server.py` passes after its `--web`-flag tests are rewritten against `Config`
- [x] 5.3 Rewrite `tests/test_web_server.py`'s parser-based assertions (for example `test_there_is_no_option_that_serves_a_wide_bind_quietly`) against the configuration surface instead; verify the rewritten tests still fail when the warning is made suppressible

## 6. The panel's tests stand on their own

- [x] 6.1 Rewrite `tests/test_web_write_parity.py` so each panel write asserts against the repository call and its stored result directly, with no `cli.main` half; verify the file imports nothing from `sighop.cli` and the whole file passes
- [x] 6.2 Rewrite `tests/test_entity_store.py`, `tests/test_first_transmit.py`, `tests/test_runtime.py`, `tests/test_runtime_persistence.py` and `tests/test_web_server.py` to construct `Runtime` or the repositories directly instead of calling `cli.main`; verify `grep -rn "sighop.cli" tests/` returns nothing for these files and each passes
- [x] 6.3 Delete `tests/test_bot_cli.py`, `tests/test_room_cli.py`, `tests/test_channel_cli.py`, `tests/test_webhook_cli.py`, `tests/test_web_user_cli.py` and `tests/test_monitor.py`; verify the full suite passes and coverage of each deleted file's subject exists in the corresponding panel test

## 7. The command line is deleted

- [x] 7.1 Delete `src/sighop/cli.py` and `src/sighop/monitor/run.py`; verify `grep -rn "sighop.cli\|MonitorRun" src/ tests/` returns nothing and `uv run pytest` passes
- [x] 7.2 Delete the names in `src/sighop/monitor/render.py` that only the monitor command called, keeping everything `runtime.py` imports; verify `uv run ruff check src` reports no unused definitions and the suite passes
- [x] 7.3 Delete `src/sighop/radio/probe.py`'s command-only helpers if any remain unused, and confirm `src/sighop/radio/capture.py` and `src/sighop/radio/replay.py` are still reachable from the runtime and the tests; verify by import graph rather than by assumption
- [x] 7.4 Rewrite every user-facing string in `src/` and every docstring that tells an operator to run a `sighop <command>` — error messages, refusals, panel copy and templates — to say what to do now (restart to migrate, use the panel, or, for accounts, that no surface exists yet); verify `grep -rn "sighop \(run\|keys\|room\|bot\|webhook\|channel\|web user\|db\|database\|capture\|monitor\)" src/` returns nothing

## 8. Replay survives as a module

- [x] 8.1 Add a `__main__` entry to `src/sighop/replay.py` taking exactly one capture path, opening no database and no modem (design D5); verify `uv run python -m sighop.replay captures/<one>.jsonl` renders the capture and exits zero with `DATABASE_URL` unset
- [x] 8.2 Make it exit non-zero with a clear message for no path, two paths, and a malformed capture line; verify three tests in `tests/test_replay.py`
- [x] 8.3 Confirm the module never prints a periodic status line, so its output is stable for a byte-for-byte diff; verify by running the same capture twice and comparing

## 9. Image, deployment and build

- [x] 9.1 Remove `CMD ["--help"]` from the `Dockerfile` and confirm the entry point starts the node with no arguments; verify `docker run` with a complete environment boots and with an empty one refuses
- [x] 9.2 Replace `compose.yaml`'s `command:` block with the equivalent `environment:` entries; verify `docker compose config` renders and the service carries no command
- [x] 9.3 Rewrite `build.sh`'s `smoke` gate as an import check plus a start-and-refuse check under the same constrained `docker run` (design D7); verify the gate passes on a good image and fails on one missing a template
- [x] 9.4 Rewrite `build.sh`'s `replay` gate to drive `python -m sighop.replay` on the host and in the image with no database on either side; verify the gate passes and still fails when the two renderings are made to differ
- [x] 9.5 Run `./build.sh` end to end and verify every gate passes
- [x] 9.6 Give CI's test gate a database — the suite now refuses to run without one, and the workflow configured none — as a `services: postgres` container pinned by digest, on a non-UTC zone because one test proves timestamps survive such a server; verify a static test in `tests/test_deployment_files.py` asserts the service, its pin and `SIGHOP_TEST_DATABASE_URL`

  *Verification in this environment (no container engine in the development container):* 9.1–9.4
  are checked by `tests/test_deployment_files.py`, by parsing `compose.yaml` and checking its
  interpolated default through the node's own validation, and by running `./build.sh smoke replay`
  against a stand-in `docker` that executes the same entry points on the host — both gates pass, and
  each fails as it should for an image that starts instead of refusing, refuses without the `openssl`
  line, cannot import, or renders a capture differently. `./build.sh lock format lint types test`
  passes (2581 tests). 9.5 was then confirmed by the operator on the host: `./build.sh` passes
  every gate against a real image. 9.6 has not yet run in CI.

## 10. Documentation and specs

- [x] 10.1 Update `DESIGN.md` §10 (container and deployment) and §11 (repository layout) for the removed command line and the required database; verify no section still describes a `sighop <command>` invocation
- [x] 10.2 Update `.devcontainer/README.md` and `.devcontainer/post-create.sh` for the container-started test database and the removed commands; verify the dev container's documented test command works inside it
- [x] 10.3 Record the account-management gap as a follow-up change proposal so it is not lost; verify the proposal exists under `openspec/changes/`
- [x] 10.4 Run `uv run ruff check`, `uv run ruff format --check`, `uv run mypy src tests` and the full `uv run pytest`, and `openspec validate --specs --strict`; verify all pass before the change is considered complete
