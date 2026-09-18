# Tasks

Commit boundaries matter here: groups 1, 2 and 3 are one commit each, and group 2 must contain
nothing but the reformat (design D3, D4).

## 1. Green the two failing gates

- [x] 1.1 Sort the imports in `src/sighop/net/dm.py`, `src/sighop/net/room.py` and
      `tests/test_dm.py` by running `uv run ruff check --fix` on those three files; verify
      `uv run ruff check` reports "All checks passed" and that `git diff` touches only import
      lines in those three files
- [x] 1.2 In `tests/test_web_write_parity.py`, replace `route.path` in the assertion message of
      `test_no_route_removes_a_stored_identity` with `getattr(route, "path", route)`, matching the
      `getattr(route, "endpoint", None)` idiom the same loop already uses (design D5); verify
      `uv run mypy` reports no errors
- [x] 1.3 Verify the suite still passes with `uv run --locked pytest -q` (expect 1938 passed,
      395 skipped) and commit groups 1.1–1.2 as a single commit

## 2. Reformat the repository

- [x] 2.1 Run `uv run ruff format .` and verify `uv run ruff format --check .` reports every file
      already formatted and that `git diff --stat` shows 101 files changed, with no file under
      `related-repos/`
- [x] 2.2 Verify the reformat changed no behaviour: `uv run --locked pytest -q` passes with the
      same counts as 1.3, `uv run ruff check` still passes, and `uv run mypy` still passes
- [x] 2.3 Commit the reformat on its own, with no other change in the commit; verify with
      `git show --stat HEAD` that the commit contains only the reformatted files

## 3. Make formatting a build gate

- [x] 3.1 Add a `format()` gate to `build.sh` running `uv run --locked ruff format --check`
      (design D1, D2), add `format) gate format format ;;` to the `run_gate` case, add `format` to
      the unknown-gate message's list, and add `gate format format` to the default sequence ahead
      of `gate lint lint`; verify `./build.sh format` passes and `./build.sh notagate` names
      `format` among the known gates
- [x] 3.2 Add `format  ruff format --check` to the gate list in `build.sh`'s header comment, above
      the `lint` line, so the documented order matches the executed one; verify by reading the
      header against the default sequence
- [x] 3.3 Verify the gate actually fails: make a temporary formatting change to one file, confirm
      `./build.sh format` exits non-zero naming `format` and that the file is left unmodified, then
      revert the change
- [x] 3.4 Add `.git-blame-ignore-revs` containing the hash of the commit from 2.3 with a comment
      naming it as the repository-wide `ruff format` adoption, and the
      `git config blame.ignoreRevsFile .git-blame-ignore-revs` line (design D4); verify
      `git blame --ignore-revs-file .git-blame-ignore-revs src/sighop/net/dm.py` attributes the
      collapsed lines to their original commits rather than to the reformat
- [x] 3.5 Commit group 3 and verify the whole build from a clean tree with `./build.sh`, confirming
      it reports the image it built and that `format` ran first

## 4. Write the missing spec Purposes

Edited directly in the main specs, not through deltas (design D7).

- [x] 4.1 Replace the `TBD - created by archiving change milestone-4-first-transmit` line in
      `openspec/specs/contacts/spec.md` with a Purpose describing what the capability covers —
      how adverts become contacts and what the system trusts about them — drawn from its own
      requirements; verify `openspec validate contacts --type spec --strict` passes
- [x] 4.2 Do the same for `openspec/specs/direct-messaging/spec.md`; verify
      `openspec validate direct-messaging --type spec --strict` passes
- [x] 4.3 Do the same for `openspec/specs/local-identity/spec.md`; verify
      `openspec validate local-identity --type spec --strict` passes
- [x] 4.4 Verify `openspec validate --all --strict` reports 45 passed, 0 failed

## 5. Verify the change end to end

- [x] 5.1 Verify `openspec validate cleanup-build-gates-and-spec-drift --strict` passes, so the
      three delta specs in this change are well-formed before they are ever synced
- [x] 5.2 Confirm the `entity-store` and `local-identity` deltas still match the main specs they
      modify — each `### Requirement:` header in the delta appears verbatim in
      `openspec/specs/<capability>/spec.md` — since group 4 edited `local-identity`'s Purpose in
      the same file
- [x] 5.3 Confirm no requirement describing the removed 32-byte seed format was reworded: verify
      `grep -n seed openspec/specs/entity-store/spec.md openspec/specs/local-identity/spec.md`
      still reports the removed-format requirements and their scenarios (design D6)
- [x] 5.4 Confirm the three scenario titles design D8 leaves alone are still present verbatim in
      the deltas — "A room server seed at rest", "A bot seed at rest" in `entity-store`, and that
      `local-identity`'s delta carries only the interchange-format requirement — since a MODIFIED
      block that omits or renames a scenario fails `openspec validate`
- [x] 5.5 Run `./build.sh` once more from a clean checkout and verify it exits 0
