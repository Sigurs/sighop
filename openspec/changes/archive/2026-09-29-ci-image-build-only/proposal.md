# Proposal

## Why

The GitHub workflow runs every `build.sh` gate — lock, format, lint, types, test (with a Postgres
service), image, smoke, replay, scan — on every pull request and every push to `master`. Those
gates already run locally before a change is committed, so repeating them on the arm64 runner
spends CI minutes for no new signal. The operator wants CI to build the image and nothing else.

## What Changes

- The workflow runs only the `image` gate (`./build.sh image`) instead of the full `./build.sh`,
  on pull requests, pushes to `master` and manual runs alike.
- The `postgres` service container and `SIGHOP_TEST_DATABASE_URL` are removed from the job; no gate
  that runs in CI needs a database.
- Publishing is unchanged: on a push to `master` or a manual run the built image is still pushed
  as `<commit>-<YYYYMMDD>-<HHMMSS>`, announced on Discord, and pruned to the newest three versions.
- **BREAKING (process)**: an image pushed to GHCR is no longer one that passed lint, types, tests,
  smoke, replay or scan in CI. Those guarantees now rest on the local `./build.sh` run. The local
  script itself is unchanged — `./build.sh` with no arguments still runs every gate.
- Tests pinning the workflow's shape and DESIGN.md's description of CI are updated to match.

## Capabilities

### New Capabilities

_None._

### Modified Capabilities

- `build-script`: requirement "Continuous integration builds every change and publishes the main
  branch" changes from running every gate in CI to running only the image build, while publishing
  behaviour stays the same.

## Impact

- `.github/workflows/build.yml`: build step, service block, header comment.
- `tests/test_deployment_files.py`: `test_the_workflow_runs_build_sh_and_pins_every_action_by_commit`
  and `test_the_workflow_gives_the_test_gate_a_database` change.
- `DESIGN.md` CI paragraph and repository-tree line; `README.md` unaffected (says only that CI
  builds and pushes).
- `build.sh`: no change. Its partial-run banner ("this is NOT a verified image") will appear in
  the CI log, which is accurate.
