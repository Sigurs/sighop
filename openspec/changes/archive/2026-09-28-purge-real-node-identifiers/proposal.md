# Proposal

## Why

The `synthetic-corpus` purge removed the recordings and redacted every name and key on its redaction
list, but that list was built from ADVERT frames only. Old versions of the golden file still carry
real node identifiers. They come from frames the list never looked at: discovery responses and
anonymous-request senders.

A fresh inventory decoded every ADVERT, ANON_REQ and discovery response in the pre-purge backup's
captures. It found **17** real public keys, and the redaction list had held 15.

Every revision of every file, on every ref, was then searched for those keys: full, as 8- and
16-hex prefixes, as 3-byte (6-hex) prefixes, and base64-encoded. The search also covered the real
advert positions. Every remaining hit is in the six recorded-era versions of
`tests/protocol/corpus_golden.txt` (commits `6205299` through `506a522`):

- two full 32-byte keys: `claimed_key=` in discovery responses (86 lines) and an ANON_REQ `sender=`
- node hashes of real nodes, 1 to 3 bytes, in `dest=`, `src=` and `path=`, and in short
  `claimed_key=` prefixes
- seven real advert positions in `loc=`

Nothing else in history matches, and the current tree is clean because its golden file is
generated from the synthetic cast.

The repository has no remote yet. Rewriting history now, before the first push to GitHub, means
these commits are never published. Doing it after the push would mean a force-push, and the old
commits would linger in forks and caches.

## What Changes

- Rewrite the six recorded-era golden blobs in place, in every commit that contains them:
  - Replace every real public key and key prefix with a synthetic stand-in. Each real key maps to
    one freshly generated Ed25519 public key.
  - Replace every node hash in `dest=`, `src=`, `path=`, `key=`, `claimed_key=` and `sender=`,
    down to 1 byte. A hash that is a prefix of a known real key becomes the same-length prefix of
    that key's stand-in. Any other hash is mapped through a secret, discarded mapping.
  - Replace every non-zero `loc=` with a synthetic position.
  - Leave all other fields untouched: line count, field order, ciphertext digests, MACs, checksums,
    tags, channel hashes, transport codes, timestamps, and existing `[redacted]` markers.
- Leave every other blob byte-identical. The tip tree does not change, because its golden file is
  already synthetic. Commit hashes from `6205299` onward do change.
- Follow the confirm-first procedure used for the previous purge:
  1. Commit the pending work.
  2. Make a backup bundle outside the repo.
  3. Rewrite a fresh clone.
  4. Verify the clone.
  5. **Stop for the operator's explicit confirmation.**
  6. Replace the repository, expire the reflog and prune.
- Report what git cannot clean:
  - The old and new backup bundles, and the old `~/sighop-purge/` redaction list. That list holds
    real names and the old `.env.prod` credentials in plain text.
  - Session transcripts and the CCE session memory, which quote key prefixes.
  - Commit hashes cited in documents and in recorded decisions. These go stale.

Not in scope:
- 1- and 2-byte hashes outside the golden file. Raw frames in old test files carry path bytes that
  cannot be told apart from other hex; the search found no real key or 3-byte prefix in them.
- Ciphertext digests, and anything already `[redacted]`.
- Force-pushing: there is no remote.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `synthetic-corpus`: add a requirement that no real node identifier (public key, key prefix, node
  hash or advert position) from the recording is reachable in any revision. It sits beside the
  existing "History carries no real data" requirement, which covers only what is on the operator's
  redaction list. The capability is introduced by the complete but unarchived `synthetic-corpus`
  change, so archive that change first.

## Impact

- **Git history:** every commit from `6205299` (milestone 1) to the tip gets a new hash. Branches:
  `master` only. Tags, stashes, other clones: none.
- **Code and tests:** none change. The tip tree is identical, so the suite and the image are
  unaffected.
- **Docs and memory:** short commit hashes cited in `openspec/changes/**`, DESIGN.md and CCE
  decisions stop resolving. They are listed, not rewritten.
- **Outside git:**
  - The backup bundles and the old `~/sighop-purge/` directory hold the real data until the
    operator deletes them.
  - The rewrite's secret and key mapping are discarded, never written to the repo.
