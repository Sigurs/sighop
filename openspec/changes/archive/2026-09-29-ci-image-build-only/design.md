# Design

## Context

`.github/workflows/build.yml` has one job on `ubuntu-24.04-arm`: checkout, `setup-uv`, name the
image, `./build.sh` (all nine gates) with a digest-pinned `postgres:18` service on
`TZ=Europe/Helsinki`, then — non-PR only — GHCR login, push, Discord notify, keep-3 prune.
`build.sh` already accepts gate names as arguments (`./build.sh image`), runs only those, honours
`$IMAGE`, and prints a "partial build … NOT a verified image" banner before exiting 0.
`tests/test_deployment_files.py` pins the workflow's shape, including the Postgres service.

## Goals / Non-Goals

**Goals:**
- CI runs only `docker build` (via the `image` gate) on every trigger.
- Push / notify / prune behaviour on `master` and manual runs is byte-for-byte unchanged.

**Non-Goals:**
- Changing `build.sh` or its default full-gate run.
- Adding a separate lint/test workflow, caching, or a schedule-based full build.
- Multi-arch images.

## Decisions

1. **Call `./build.sh image`, not `docker build` directly.** The `image` gate owns the build
   arguments (`SIGHOP_VERSION` from `uv version`, `SIGHOP_COMMIT` from git) and
   `--provenance=false --sbom=false`, which the keep-3 retention depends on. Duplicating that
   invocation in YAML would let the two drift. Cost: `setup-uv` stays, because `build.sh`
   computes the version with `uv version` before any gate runs.
2. **Same behaviour for pull requests as for pushes.** The request was "only run docker build";
   PRs get the image build as a Dockerfile/dependency sanity check and nothing else. Assumption
   recorded here since PR checks become much weaker; revisit if PRs from others start arriving.
3. **Drop the `postgres` service and `SIGHOP_TEST_DATABASE_URL`.** Nothing in CI needs a
   database any more; starting one would cost the health-check wait for nothing.
4. **Keep `build.sh`'s partial-build banner.** It is accurate — the pushed image did not pass the
   other gates — and changing `build.sh` for cosmetics is out of scope.
5. **Tests invert rather than disappear.** `test_the_workflow_gives_the_test_gate_a_database`
   becomes a test that the workflow runs exactly `./build.sh image` and declares no `services:`,
   so a later edit that silently re-adds the full run is caught.

## Risks / Trade-offs

- [An image pushed to GHCR may fail lint, tests, smoke or replay] → Operator runs `./build.sh`
  locally before pushing to `master`; the `build-script` spec's full-gate requirements still apply
  to the local run. The deployment pulls images by tag, so a bad push is rolled back by redeploying
  the previous tag (three are kept).
- [No vulnerability scan in CI] → Local `./build.sh` still scans; scan never failed the build
  anyway, so only the log record in CI is lost.
- [Musl-vs-glibc divergence is no longer checked in CI] → Same mitigation: replay gate locally.
