# Tasks

## 1. Workflow

- [x] 1.1 In `.github/workflows/build.yml`, change the build step to `run: ./build.sh image` (rename it e.g. "Build the image (build.sh image)"), drop its `SIGHOP_TEST_DATABASE_URL` env, keep `IMAGE`; verify with `grep -n "build.sh" .github/workflows/build.yml` showing only `./build.sh image`
- [x] 1.2 Remove the `services: postgres:` block and its comment; verify `python3 -c "import yaml; d=yaml.safe_load(open('.github/workflows/build.yml')); assert 'services' not in d['jobs']['build']"` passes
- [x] 1.3 Rewrite the header comment: CI builds only the image; other gates run locally via `./build.sh`; publish/notify/keep-3 unchanged; verify comment no longer mentions the test database or "every gate"

## 2. Tests

- [x] 2.1 In `tests/test_deployment_files.py`, make `test_the_workflow_runs_build_sh_and_pins_every_action_by_commit` assert `run: ./build.sh image` is the only `build.sh` invocation; verify with `uv run pytest -q tests/test_deployment_files.py`
- [x] 2.2 Replace `test_the_workflow_gives_the_test_gate_a_database` with a test that the workflow declares no `services:` and no `SIGHOP_TEST_DATABASE_URL`; update the "build and verify" comment in the tags test; verify same pytest command passes

## 3. Docs

- [x] 3.1 Update DESIGN.md CI paragraph (~line 1699) and the repository-tree line for `.github/workflows/` (~line 1838) to say CI runs only the image gate with no Postgres; verify `grep -n "Postgres for the test gate\|runs \`./build.sh\` on every" DESIGN.md` returns nothing

## 4. Verification

- [ ] 4.1 Run `./build.sh lint` and `uv run --locked pytest -q` (full suite, database from `.env.dev`) and confirm both pass
- [x] 4.2 Run `openspec validate ci-image-build-only --strict` and confirm it passes
- [ ] 4.3 After merge, confirm the next GitHub Actions run on `master` shows only the image gate, no Postgres service, and still pushes, notifies and prunes (operator check)
