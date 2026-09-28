# Tasks

Work files, including the key set, scripts, tables and bundle, live in `~/sighop-purge/` (mode 700)
or the session scratchpad. They never go in the repository (design D1, D7).

## 1. Before anything is rewritten

- [ ] 1.1 Archive `synthetic-corpus` first, so the `synthetic-corpus` capability exists in `openspec/specs/` for this change's ADDED requirement. Then commit all pending work, including `.gitleaks.toml`, README, build.yml, DESIGN.md and this change. Verify `git status` is clean and `openspec validate purge-real-node-identifiers --strict` passes.
- [ ] 1.2 Build the real key set (D1): clone `~/sighop-purge/sighop-pre-purge.bundle` to the scratchpad, extract `captures/` at `bbd9bc3^`, and decode every `rx_frame`/`tx_frame` with `decode`/`parse_payload`. Collect ADVERT `public_key`, ANON_REQ `sender_public_key` and `DiscoverResponse.claimed_key`. Write the result to `~/sighop-purge/real-keys.txt`. Verify it lists 17 keys; report any other count before going on.
- [ ] 1.3 Collect the real positions: every distinct non-zero `loc=` pair in the six recorded-era golden blobs (design, Context table). Write them to `~/sighop-purge/real-locs.txt`. Verify the count is 7.
- [ ] 1.4 Re-run the inventory against the live repository. For each key in 1.2, search every revision (`git grep -I -i -F` over `git rev-list --all`) for the full hex, the 16-, 8- and 6-hex prefixes and the base64 form. Do the same for each 1.3 position. Verify every hit is in one of the six blobs in the Context table; stop and report if anything else matches.
- [ ] 1.5 In the six blobs, count node-field values (`key=`, `claimed_key=`, `sender=`) of 8+ bytes that match no key in 1.2 (D6 risk). Verify the count is zero; otherwise re-derive the key set before going on.
- [ ] 1.6 Make the backup: `git bundle create ~/sighop-purge/sighop-pre-node-purge.bundle --all`. Verify `git bundle verify` passes, and tell the operator this bundle holds the real identifiers.

## 2. Rewriter

- [ ] 2.1 Write the stand-in generator in `~/sighop-purge/`. For each of the 17 keys, draw a stand-in `LocalIdentity.from_seed(os.urandom(32)).public_key`. Redraw it if its first 3 bytes equal any real key's first 3 bytes, or if it equals a `synthetic-corpus` cast key (D3). Keep the seeds in memory only. Verify by an in-process assertion over the 17 pairs.
- [ ] 2.2 Write the field-aware rewriter for recorded lines, covering `key=`, `claimed_key=`, `sender=`, `dest=`, `src=`, `path=` split by `hops=<count>x<size>`, and non-zero `loc=`. It maps attributable prefixes to stand-in prefixes (D3), everything else through HMAC under an in-memory `os.urandom(32)` secret (D4), positions to ordered placeholders (D5), and leaves `[redacted]` and every other field alone. Verify on a hand-written line for each field form, including a partly redacted path, that the output matches expectations.
- [ ] 2.3 Rewrite the six blobs in one process, so stand-ins are consistent across them. Run the D6 shape check on each pair: the same line count, the same field-name sequence per line, each hex value at the same length, other fields byte-identical. Write `~/sighop-purge/blob-table.json` (original blob id → new content), then let the process exit, which discards the secret and seeds. Verify all six pass and that the table holds exactly the six ids from the Context table.

## 3. Rewrite a clone

- [ ] 3.1 `git clone --no-local . ~/sighop-purge/clone`, then in the clone run `uv tool run git-filter-repo --blob-callback` with a callback that replaces `blob.data` when `blob.original_id` is in the table and passes every other blob through unchanged (D2). Verify it exits zero and `master` exists.

## 4. Verify the clone (independent of the rewriter)

- [ ] 4.1 Search the clone's every revision for each 1.2 key (hex either case, base64) and its 16-, 8- and 6-hex prefixes, and for each 1.3 position. Verify there are no hits; report any hit rather than fixing it silently.
- [ ] 4.2 Compare object sets: every blob in `git rev-list --objects --all` of the original, except the six, is present in the clone, and the clone has exactly six blobs the original lacks. Also check the tip tree hash is equal, the commit count is equal (43 at planning time, plus the 1.1 commits), and `git log --format='%an%x00%ae%x00%ad%x00%B'` is identical. Verify all checks pass.
- [ ] 4.3 Read one rewritten golden revision by eye: `git show <new 506a522>:tests/protocol/corpus_golden.txt | head -40`. Verify the fields keep their shape, and grep the clone for the planning-time HMAC and key tables to confirm none were committed (spec: *Stand-ins cannot be reversed*).
- [ ] 4.4 Show the operator the results of 4.1–4.3 and the old-to-new commit map summary (`.git/filter-repo/commit-map`: how many commits changed, first changed commit).

## 5. Swap (destructive — stop for the operator)

- [ ] 5.1 STOP: proceed only on the operator's explicit confirmation. Then rename the original to `sighop.pre-node-purge`, move the clone into place, and restore the working-tree-only files the clone lacks (`.env`, `.env.dev`, `.env.prod`, `keys/`, `.venv`, `related-repos` and other ignored or untracked state) from the renamed original. Verify `git status` is clean, `git log --oneline | wc -l` matches 4.2, and `git branch -a` shows only `master`.
- [ ] 5.2 In the swapped repo, run `git remote remove origin` (the clone points at the old path), then `git reflog expire --expire=now --all && git gc --prune=now --aggressive`. Verify `git fsck --unreachable --no-reflogs` prints nothing and 4.1's search still finds nothing.
- [ ] 5.3 Run lint and tests on the tip: `uv run --locked ruff format --check`, `uv run --locked ruff check`, `uv run --locked mypy`, `uv run --locked pytest -q`. Verify all are green. The tree is unchanged, so any failure is environmental and must be reported.
- [ ] 5.4 Once 5.1–5.3 pass, delete the renamed original directory. Verify that it is gone and that the backup bundle from 1.6 is still present.

## 6. Stale references

- [ ] 6.1 List the short commit hashes cited in the tip tree (`openspec/`, `DESIGN.md`, `tests/protocol/CORPUS.md`) and in CCE recorded decisions that no longer resolve (`git cat-file -e`). Show them to the operator with their new hashes from the commit map. Verify the list is shown; do not rewrite them unless the operator asks.

## 7. Report what git cannot clean

- [ ] 7.1 Report to the operator, one item each:
  - Delete `~/sighop-purge/`: the old bundle, the new bundle, `redactions.txt` (plain-text real names and the old `.env.prod` credentials), `real-list.txt`, `real-keys.txt`, `real-locs.txt`, `blob-table.json` and the clone remnants.
  - Confirm the `.env.prod` secrets from the previous purge were rotated.
  - Decide about Claude session transcripts and CCE session events, which quote key prefixes and, from the planning session, the redaction list's credential lines.
  - Run `cce index` on the host.
  - Push to GitHub only after this.

  Verify the operator has been shown each item.
