# Proposal

## Why

`build.sh` is red at `HEAD` (f96c893). Its lint gate fails on three `I001` import-sort errors and
its type gate fails on one `attr-defined` error, so every pull request and every push to the main
branch currently stops before the image is built. Separately, three capabilities still carry the
placeholder `## Purpose` that `openspec archive` wrote for them, and two capabilities describe key
material as a "seed" in requirements whose own headings say "private key" — wording the
private-key change left behind because a delta merges the requirements it touches and no delta
touched these. The specs now contradict both the code and themselves.

The build failures are the reason for the timing: the repository cannot produce a verified image
until they are fixed, and the surrounding cleanup is cheap to land in the same pass.

## What Changes

- Fix the three `I001` import-sort errors (`src/sighop/net/dm.py`, `src/sighop/net/room.py`,
  `tests/test_dm.py`). All three are the same cause: the `wait_for_readback` / `NoRadioReadback`
  imports were appended rather than sorted in.
- Fix the `"BaseRoute" has no attribute "path"` error in `tests/test_web_write_parity.py`. The
  assertion message reads `route.path` off a Starlette `BaseRoute`, which only `Route` and `Mount`
  define.
- Adopt `ruff format` across the repository and add `ruff format --check` to `build.sh` as a gate,
  so the 101 files that have drifted are brought into line once and cannot drift again.
- Write real `## Purpose` sections for `contacts`, `direct-messaging` and `local-identity`, so
  `openspec validate --all --strict` passes for every item rather than 42 of 45.
- Correct the requirements that describe stored key material as a "seed" where the system holds a
  64-byte private key, in `entity-store` and `local-identity`. The requirements that describe the
  *removed* 32-byte seed format are correct as they stand and are left alone.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `build-script`: the fixed gate order gains a formatting gate, and the requirement that names the
  gates is updated to include it. A tree whose formatting differs from `ruff format` fails the
  build the way a lint violation does.
- `entity-store`: four requirements describe stored, inspected and sealed key material as a
  "seed". The system seals a 64-byte private key (`seal_private_key`, the `sealed_private_key`
  column), and two of those requirements are already titled "...private key is protected exactly
  as any other". Their text is corrected to say private key.
- `local-identity`: the requirement that a keyfile is an interchange format says the system SHALL
  NOT write an identity's "seed" to a keyfile, and two of its scenarios follow it. Corrected to
  private key.

Three scenario *titles* are deliberately left as they are — "A room server seed at rest", "A bot
seed at rest", and `local-identity`'s "Public key stored in the file matches the seed", which
describes its own body wrongly and did so before this change. A delta cannot rename a scenario: a
MODIFIED block must reproduce every existing scenario name or validation rejects it. Their
requirement text and bodies are corrected here; the labels are a follow-up, and design.md — D8
records why.

## Impact

Code and configuration:

- `src/sighop/net/dm.py`, `src/sighop/net/room.py`, `tests/test_dm.py` — import order.
- `tests/test_web_write_parity.py` — one assertion message.
- `build.sh` — a new `format` gate ahead of `lint`.
- 101 files across `src/`, `tests/` and `alembic/versions/` — reformatted by `ruff format`. The
  diff is 1191 lines removed and 947 added, and every hunk is the same kind: a call or signature
  that fits within the configured 100-column limit collapsed onto one line.

Specs:

- `openspec/specs/contacts/spec.md`, `openspec/specs/direct-messaging/spec.md`,
  `openspec/specs/local-identity/spec.md` — `## Purpose` written directly in the main spec. A
  `## Purpose` in a delta is read only when a capability is created, so it cannot replace an
  existing one.
- `openspec/specs/entity-store/spec.md`, `openspec/specs/local-identity/spec.md` — requirement
  wording, through delta specs.

No behaviour of the running system changes. No dependency is added: `ruff` is already the lint
tool and `ruff format` ships in the same binary, already pinned by `uv.lock`.

Not affected: the test suite passes at `HEAD` (1938 passed, 395 skipped) and `uv lock --check` is
clean, so the lock, test, image, smoke, replay and scan gates are untouched by this change.
