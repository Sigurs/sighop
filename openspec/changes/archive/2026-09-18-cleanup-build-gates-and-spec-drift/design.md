# Design

## Context

See proposal.md — Why. Three facts shape the approach:

- `build.sh` declares its gates as shell functions and dispatches them through a `run_gate` case
  and a straight-line default sequence (`gate lock lock`, `gate lint lint`, …). Adding a gate is
  four edits in one file, and `build-script`'s spec already fixes the order and the stop-at-first-
  failure behaviour.
- The formatter has never been run here. `ruff format --check` reports 101 of 461 files, 1191 lines
  removed and 947 added. Every hunk is the same shape: a call or signature that fits inside the
  configured 100-column limit, collapsed onto one line. `related-repos` is already excluded by
  `[tool.ruff] extend-exclude`, so the vendored clones are untouched.
- The deliberate hand-formatting the `pyproject.toml` comments defend — the `if`/`elif` chains over
  byte values in the codec — is a *lint* concern (`SIM102`, `SIM108`, both ignored). The formatter
  does not restructure those. It also honours the magic trailing comma, so literals and signatures
  that were deliberately exploded with a trailing comma stay exploded.

## Goals / Non-Goals

**Goals:**

- `./build.sh` runs green from a clean checkout of the resulting commit.
- Formatting drift cannot silently accumulate again.
- The 101-file reformat is separable from every semantic change, in the history and in review.
- `openspec validate --all --strict` reports 45 of 45.

**Non-Goals:**

- Changing any lint rule, any `ignore` entry, or `line-length`. The formatter runs on the existing
  configuration.
- Reformatting `related-repos`, which is vendored and read-only here.
- Touching the requirements that describe the *removed* 32-byte seed format. Those say "seed"
  because they mean it.
- Any runtime behaviour change.

## Decisions

### D1: Formatting is its own gate, ahead of lint

`build.sh` gains a `format()` gate running `uv run --locked ruff format --check`, placed before
`lint` in the default sequence and added to the `run_gate` case and its unknown-gate message.

Why a separate gate rather than folding the check into `lint()`: `build-script`'s spec requires the
build to name the failing gate, and "lint" is the wrong name for "your formatting differs" — the
two call for different commands to fix. A separate name tells the operator to run `ruff format`
rather than to go read a rule code.

Why before lint: formatting differences are the most mechanical failure available, and reporting
them first means a contributor fixes the trivial thing before reading lint output that reformatting
may itself resolve (`I001` is the overlap).

Alternative considered: a pre-commit hook instead of a build gate. Rejected — the repository has no
pre-commit configuration and CI runs `build.sh` and nothing else, so a hook would be advisory on
the host and absent in CI, which is how the current drift accumulated.

### D2: `--check`, never a write

The gate uses `ruff format --check`, which exits non-zero and writes nothing. A build must not
modify the checkout it was asked to verify — the same principle the lock gate already applies when
it refuses to update `uv.lock`. This is stated as a requirement in the `build-script` delta so it
is not quietly relaxed later.

### D3: Fix the two red gates first, reformat second

Order of commits: (1) the three `I001` fixes and the one mypy fix, (2) the repository-wide
reformat, (3) the new gate, (4) the spec edits.

The semantic fixes land on the current formatting so their diff is four lines in four files and can
be read as such. If the reformat commit is ever reverted, the build is still green. The reverse
order would bury the only behaviour-adjacent edits in the change inside a 101-file diff.

### D4: The reformat is one commit, recorded in `.git-blame-ignore-revs`

Reformatting 101 files rewrites the blame for every line it touches. The change adds a tracked
`.git-blame-ignore-revs` file naming the reformat commit, with a comment saying what the revision
is. `blame.ignoreRevsFile` is per-clone git configuration and cannot be committed, so the file
carries the one-line command that enables it (`git config blame.ignoreRevsFile
.git-blame-ignore-revs`); GitHub's blame view honours the file without any configuration at all.

The commit hash is not known until the reformat commit exists, so the file is written in the
commit that follows it.

This requires the reformat to be exactly one commit containing nothing else — which D3 already
demands for a different reason.

### D5: The mypy fix uses `getattr`, matching the loop it sits in

`tests/test_web_write_parity.py` iterates `app.routes`, typed as `list[BaseRoute]`. `BaseRoute`
defines no `path`; `Route` and `Mount` do. The loop directly above already reaches for an optional
attribute with `getattr(route, "endpoint", None)`, so the assertion message uses the same idiom
rather than introducing an `isinstance` narrowing or a `cast` for a string that only ever appears
in a failure message.

A `# type: ignore` is not an option here: `warn_unused_ignores = true` is set, and an ignore would
assert that the attribute access is correct when it is not.

### D6: Which "seed" mentions change, and which do not

The rule the implementation follows, so a blanket search-and-replace is not attempted:

- **Correct to "private key"** where the text describes what the system does now. The system seals
  a 64-byte private key (`seal_private_key`, the `sealed_private_key` column), and `entity-store`'s
  own Purpose already says so. Two of the four affected requirements are titled "...private key is
  protected exactly as any other" over a body that says "seed".
- **Leave alone** where the text describes the removed 32-byte seed format: `entity-store`'s "Key
  material stored under the removed format is refused by name", its "Removing a row under the
  removed format" scenario and the "under the removed seed format" clause in the removal
  requirement, and `local-identity`'s scenario asserting that a keyfile recording a seed is
  refused. These are correct as written.

### D7: Purpose sections are edited in the main specs, not proposed as deltas

A `## Purpose` in a delta is read only when archiving *creates* a capability, so a delta cannot
replace the `TBD` placeholders on `contacts`, `direct-messaging` and `local-identity`. Those three
edits are made directly in `openspec/specs/<capability>/spec.md`. This is why the proposal lists
`contacts` and `direct-messaging` under Impact rather than under Modified Capabilities: neither has
a requirement changing, and inventing one to carry the Purpose would put a requirement in a main
spec that no change proposed.

### D8: Scenario titles are left alone, because a delta cannot rename one

OpenSpec 1.13.1 treats a scenario name as its identity inside a requirement. A `## MODIFIED`
block must reproduce every scenario name the current spec has, or `openspec validate` rejects it —
so a delta can change a scenario's body but cannot rename it. `## RENAMED Requirements` operates on
requirements, not scenarios.

Three titles therefore keep wording this change would otherwise correct:

- `entity-store`: "A room server seed at rest" and "A bot seed at rest". Their requirement text and
  their bodies are corrected; only the labels above them still say seed.
- `local-identity`: "Public key stored in the file matches the seed", which asserts that a keyfile
  under the removed format is refused and so matches nothing in its own title. This is the one
  genuinely misleading label of the three, and it predates this change.

Because nothing else in `local-identity`'s "An entity identity survives the process" requirement
changes, that requirement is not carried in the delta at all — a MODIFIED block reproducing it
verbatim would assert a change that is not there.

The alternatives were both worse. Removing and re-adding the requirements to force new titles needs
a `**Reason**` and `**Migration**` for requirements that are not going away, and reads in the
archive as a deletion. Editing the titles directly in the main specs — the escape hatch D7 uses for
Purpose — would break this change's own deltas, whose MODIFIED blocks carry the old titles, and
could only be done safely after this change is archived. Left as a follow-up rather than smuggled
in: it is three labels, and the normative text is what this change was for.

## Risks / Trade-offs

- **The reformat conflicts with work in flight** → The only open change,
  `devcontainer-dev-environment`, is complete (34/34 tasks) and its files are `.devcontainer/`
  shell and JSON, which the Python formatter does not touch. Land the reformat as one commit so any
  future conflict resolves by re-running `ruff format` rather than by hand.
- **`ruff format` changes behaviour somewhere** → It is AST-preserving, and the full suite (1938
  passed, 395 skipped) runs after the reformat commit and again through the build's own test gate.
  The replay gate additionally compares rendered capture output, which would catch a formatting
  change that altered a string literal.
- **A future `ruff` upgrade reformats again and the gate turns red on an unrelated PR** → `ruff` is
  pinned in `uv.lock` and the gate runs `--locked`, so the formatter version changes only when the
  lock does, which is already a deliberate, reviewed commit.
- **`git blame` becomes less useful for the 101 files** → D4's `.git-blame-ignore-revs`. This is a
  real, permanent cost accepted in exchange for the gate; it is the reason the reformat is isolated
  to one commit.
- **Correcting spec wording without a behaviour change reads as churn** → The delta is the vehicle
  that makes it reviewable: the requirements currently contradict their own headings and the code,
  and a reader cannot tell which of the two the system actually implements.

## Migration Plan

No deployment. The order is the commit order in D3, and the change is complete when `./build.sh`
runs green from a clean checkout and `openspec validate --all --strict` reports 45 of 45.

Rollback: each commit is independently revertible. Reverting the gate commit alone leaves the tree
formatted and the build green; reverting the reformat alone requires also reverting the gate, since
the gate would then fail on the restored drift.
