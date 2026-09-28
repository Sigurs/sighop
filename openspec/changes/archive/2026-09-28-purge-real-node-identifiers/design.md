# Design

## Context

This builds on `synthetic-corpus` (D9–D12 in its design). That change removed `captures/`, the burned
fixture and `.env.prod` from history, and replaced its redaction list with `[redacted]` in every blob.
See proposal.md for what is left and why.

State at planning time:

- **History:** one branch (`master`), 43 commits, no tags, stashes or remote.
- **Where the leftovers are:** the six recorded-era versions of `tests/protocol/corpus_golden.txt`,
  listed below. The tip's golden file is synthetic. Every other blob in history was searched and is
  clean (proposal.md, Why).

| Commit | Blob | Recorded lines | Distinct `loc=` |
|---|---|---|---|
| `6205299` | `1e7207f` | 351 | 5 |
| `402dda7` | `f3f1365` | 442 | 5 |
| `8c4428e` | `bae54fb` | 997 | 7 |
| `67073ee` | `96a4010` | 1003 | 7 |
| `762288e` | `646ddc5` | 1093 | 7 |
| `506a522` | `e8a08ed` | 1093 | 7 |

- **The recording:** the pre-purge backup `~/sighop-purge/sighop-pre-purge.bundle` still holds
  `captures/` at `bbd9bc3^`. It is the only source for the real key set.
- **Golden line format:** each recorded line is
  `<file>.jsonl[NNN] route=… type=… v1 tc=… hops=<count>x<size> path=<hex|-> | <payload fields>`.
  - The node fields are `key=`, `claimed_key=` (32 or 8 bytes), `sender=`, `dest=` and `src=`
    (1 byte).
  - `path=` is `count × size` bytes. A few paths are already partly `[redacted]` from the previous
    purge.

## Goals / Non-Goals

**Goals:**
- Real node identifiers are gone from every revision (spec: *History carries no real node
  identifiers*).
- Old golden revisions still read as golden files. Fields keep their shape; they aren't blanked.
- The tip tree and every unrelated blob are unchanged, so the rewrite can't break the build.

**Non-Goals:**
- Making old commits pass their own tests. They already can't, because `captures/` is gone.
- Rewriting commit hashes cited in documents or CCE decisions.
- Touching any repository other than this one.

## Decisions

### D1. Derive the real key set by decoding the recording, not from the old list

The previous list was built from ADVERT frames, which is how two keys slipped through. Task 1
decodes every `rx_frame` and `tx_frame` in the backup's captures with sighop's own
`decode` / `parse_payload`, and collects:
- ADVERT `public_key`
- ANON_REQ `sender_public_key`
- `DiscoverResponse.claimed_key`

A planning-time run found 17 keys. The result is written only to `~/sighop-purge/` or the scratchpad,
never the repo, because the list is itself the leak.

Alternative: extend the old list by hand. Rejected, because that is the method that missed these
keys.

### D2. Rewrite by blob id with a `--blob-callback`, not `--replace-text`

`--replace-text` does literal or regex substitution across every blob. That is fine for 8+ hex
characters, but useless for 1-byte hashes: `dest=54` as a literal would also hit unrelated text, and
per-field context is needed. Instead:

1. A scratch script reads the six original blobs.
2. It builds each rewritten blob with a field-aware rewriter (D3–D5).
3. It checks each result (D6).
4. It writes an `original blob id → new content` table.
5. `git filter-repo --blob-callback` replaces exactly those six blob ids and passes everything else
   through.

Keying on the original blob id means no other file can be touched, even another golden-shaped file.
The tip's golden blob is not in the table.

This runs as `uv tool run git-filter-repo` on a fresh `git clone --no-local`, like the previous
purge. `uvx` is not on this container's PATH; `uv tool run` is the same tool.

Alternative: `--file-info-callback`, which gives path access. It needs a newer filter-repo, and
blob id is stricter than path anyway.

### D3. Stand-in keys: fresh Ed25519 keys, prefix-consistent

Each of the 17 real keys gets one stand-in: the public key of `LocalIdentity.from_seed(os.urandom(32))`.
That makes it a real, well-formed key, which matters if anyone renders an old golden. The seed is
not kept.

The rewriter treats a hex value in a node field as follows:
- A full key maps to its stand-in.
- A value of *n* bytes that is a prefix of exactly one real key maps to the first *n* bytes of that
  key's stand-in. That covers 8-byte `claimed_key=` values and path hashes of known nodes.
- A value that is a prefix of several real keys (possible at 1 byte) counts as unattributable (D4).

A stand-in must not start with the same 3 bytes as any real key, and must not equal a
`synthetic-corpus` cast key. Otherwise a "clean" scan could match by accident, or two fixtures could
alias.

### D4. Unattributable hashes: HMAC under a discarded secret

Many 1–3-byte hashes belong to nodes that never advertised in the recording, for example repeaters
on a path. Each becomes `HMAC-SHA256(secret, bytes([n]) + h)[:n]`, where the secret comes from
`os.urandom(32)` at run time and is never written anywhere.

A public seed would not work here. With 256 possible 1-byte inputs, anyone could rebuild the table
from a public seed. The mapping need not be injective: two real hashes colliding onto one stand-in is
harmless in a fixture nobody runs.

Paths are split into `size`-byte hops using `hops=<count>x<size>`. A hex run that `[redacted]` leaves
off a hop boundary is rewritten as one unattributable value of its own length.

### D5. Positions: cast-style placeholders by order of first appearance

Each distinct non-zero `loc=` pair gets a placeholder in order of first appearance:
`11000+1000·i, 23000+1000·i`. That is the same near-null-island scale the synthetic cast uses.
`loc=0,0` stays as it is. The order of first appearance reveals nothing about the real coordinates,
so this needs no secret.

### D6. Shape check per blob before filter-repo runs

For each of the six blobs, the script asserts:
- the same number of lines
- the same sequence of field names per line
- every rewritten hex value at the same length as the original
- every other field byte-identical

After the rewrite, the verification (task 4) runs independently of the rewriter, so a rewriter bug
cannot hide itself:
- Search every revision for each real key (hex in either case, base64) and each key's 3-, 8- and
  16-byte prefixes. There must be no hits.
- Check that no real `loc=` pair appears.
- Compare `git rev-list --objects --all`: every blob other than the six must be identical.
- Check that the tip tree hash and the commit count match.
- Compare author, date and message per commit.

### D7. Pending work is committed first, and the plan carries no real values

The rewrite carries the tree as committed. The uncommitted `.gitleaks.toml`, README, build.yml,
DESIGN.md and this change's files are committed before the backup, so nothing is lost in the swap.

These artifacts cite commit and blob ids and counts, never a key, prefix or position. They are safe to
commit, and to publish afterwards.

## Risks / Trade-offs

- **A rewriter bug corrupts a golden revision.** → The D6 shape check runs before filter-repo; the
  independent verification runs after it. The original stays untouched until the operator confirms,
  and there is a bundle backup.
- **The real key set is incomplete again** (a key carried by a payload type not decoded). → Inside
  the golden files this cannot leak: the rewriter replaces every node field whether or not it
  recognises the value, so an unknown key becomes an unattributable stand-in. The key set only drives
  prefix consistency and the history-wide search. The golden renders no plaintext, so no other field
  can carry a key. Task 1 also counts the node-field values in the six blobs that are 8+ bytes and
  match no known key. It must find zero, or the key set is re-derived before going on.
- **Stand-in collides with a real prefix by chance.** → D3 rejects and redraws it.
- **Every commit hash from milestone 1 on changes.** Recorded decisions and docs citing `bbd9bc3`,
  `19b3db8` and others go stale. → Accepted. Task 6 lists the citations in the tip tree; filter-repo's
  `commit-map` in the clone's `.git/filter-repo/` maps old hashes to new while the clone exists.
  Commit messages cite no hashes, so they are unchanged.
- **Backups defeat the purpose if kept.** The old bundle and redaction list, and the new bundle,
  hold real data. The old redaction list also holds the old `.env.prod` credentials in plain text.
  → Task 7 reports them for the operator to delete. Nothing is deleted without them.

## Migration Plan

1. Commit pending work.
2. Build the key set and write the backup bundle.
3. Rewrite a clone and verify it.
4. **Stop for confirmation.**
5. Swap the repository in, keeping the original directory renamed until the checks pass.
6. Expire the reflog and gc.
7. Run lint and tests on the tip.
8. Report follow-ups.

**Rollback before step 6:** rename the original back.

**Rollback after step 6:** `git clone` the backup bundle.

The first GitHub push happens only after this change completes.
