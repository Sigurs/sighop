# Proposal

## Why

The regression corpus is 13 files of live LoRa traffic recorded off a real mesh, and it is committed.
That is a privacy problem and a quality problem.

**Privacy.** The captures hold other people's node public keys and names (every ADVERT), their
Public-channel chat (the Public key is well known and the tests decrypt it, so the plaintext is one
function call away), the operator's own dev node names, and — in `tests/fixtures/burned-first-transmit.json`
— a committed private key. `.gitignore` already treats `keys/` as never-commit; the corpus and its
fixture are the exceptions. The golden file and several tests copy names and key prefixes out of the
captures as literals.

**Quality.** 1,232 recorded frames are a small number of distinct situations repeated. About a third of
receptions are flood repeats of a packet already seen, 168 frames are near-identical node-discovery
exchanges, and the 555-frame overnight file is mostly the same eleven repeaters advertising. Coverage
comes from volume, not variety, and the suite replays all of it for every test that wants "some real
traffic".

A synthetic corpus, generated from sighop's own encoders under a fixed seed, fixes both: no real
identity or message can be in it, and its composition is chosen — wide coverage, a controlled and
deliberate amount of repetition — instead of inherited from whatever the mesh did on the night.

## What Changes

- Add a **deterministic generator** that builds a synthetic mesh (fictional node names, seeded
  Ed25519 identities, fictional Public-channel chat under the real Public key, a second synthetic
  channel, synthetic direct-message / room / path / discovery / trace exchanges) and writes it as
  ordinary capture JSONL with a `capture_meta` header. Same seed, same bytes.
- Add the generated files as the new committed corpus, under `tests/corpus/`, roughly a quarter to a
  third the size of the recorded one. A small, declared amount of repetition is kept on purpose —
  flood repeats over different paths, one late echo well past 60 s, one identical DM retransmission —
  because the dedup and path-learning tests need to see them; nothing else repeats.
- Add a **drift check**: a test regenerates the corpus in memory and fails if it differs from the
  committed files, and a **no-real-data guard** that fails if a corpus record names a node or key that
  is not from the synthetic cast.
- **BREAKING (test evidence):** delete the recorded corpus (`captures/*.jsonl`, `.log`, `.meta.json`),
  the burned private-key fixture `tests/fixtures/burned-first-transmit.json`, and the recorded golden
  file; regenerate the golden file from the synthetic corpus.
- **BREAKING (test evidence):** the foreign-implementation decryption proofs — a DM produced by stock
  MeshCore firmware, and 85 recorded Public-channel frames — go with the recordings. A synthetic
  corpus can only prove sighop against itself. The specs and `CORPUS.md` say so instead of implying
  otherwise. (Signing keeps its firmware-embedded keypair anchor and the fixed known-answer vectors in
  `test_crypto.py`, neither of which involves the corpus.)
- Re-point every test that reads the corpus, and re-derive every test literal that was copied out of it
  (real node names, key prefixes, counts, the distribution and per-file expectations).
- Move the replay gate in `build.sh` (and its bind mount and deployment test) to the new location, and
  gitignore `captures/` so a live `SIGHOP_CAPTURE_FILE` recording cannot be committed by accident.
- Scrub real key prefixes and node names from `DESIGN.md`, `CORPUS.md` and archived change documents.
- **Purge git history**, as the last phase and only after the operator confirms: remove the capture
  files, the burned fixture and `.env.prod` from every revision on every branch, redact the real node
  names and key prefixes from every text blob, expire the reflog and prune. A backup bundle is kept
  outside the repo until the operator discards it. `.env.prod` held `SIGHOP_SECRET_KEY` and
  `DATABASE_URL` in six commits, so those secrets are rotated; `keys/` was never committed.

Not in scope: force-pushing or purging any host or clone. The repo has no remote configured, so
nothing is pushed; any other clone, fork or hosting cache holds the old commits until its owner
purges it, and the CCE index and Claude transcripts on this machine hold excerpts of the real
data outside git. The change lists them and says how, and does not touch them silently.

## Capabilities

### New Capabilities

- `synthetic-corpus`: the generator, its determinism and cast, the composition budget that bounds
  repetition, the drift check and the no-real-data guard.

### Modified Capabilities

- `protocol-corpus`: the corpus becomes synthetic and lives in `tests/corpus/`; provenance becomes
  "generated, with seed and generator version" rather than "recorded"; the read-only-evidence,
  first-transmit-session and recorded-count requirements are replaced; the coverage-gap requirement
  changes because the generator can produce shapes the mesh never did.
- `mesh-crypto`: the two foreign-implementation decryption requirements (direct message, Public
  channel) and the peer-acknowledgement requirement no longer have recorded evidence to stand on and
  are replaced by requirements that state what the synthetic corpus does and does not prove.

## Impact

- **Removed:** `captures/*` (13 `.jsonl`, 8 `.log`, 2 `.meta.json`), `tests/fixtures/burned-first-transmit.json`,
  `tests/protocol/corpus_golden.txt` (regenerated).
- **Added:** `tests/protocol/synthetic.py` (generator), `tests/corpus/*.jsonl`, a generator entry point,
  drift and guard tests.
- **Changed:** `tests/protocol/corpus.py`, `test_corpus.py`, `test_foreign_decrypt.py`,
  `test_channel_foreign_decrypt.py`, `golden.py`, `generate_golden.py`, `CORPUS.md`, roughly 25 test
  modules that name `CAPTURES_DIR` or a specific capture, `build.sh`, `tests/test_deployment_files.py`,
  `.gitignore`, `DESIGN.md`.
- **No change** to anything under `src/`: this is test data and test tooling, and the capture writer and
  replay reader are untouched. The `capture-replay` capability is unaffected.
- **History:** every commit hash changes, on both branches. References to hashes in notes, the CCE
  memory and any external tracker go stale.
- **Risk:** a synthetic corpus encodes our own reading of the protocol, so it cannot reveal a
  misreading the way the recording did (the multi-byte path-hash rule was found that way). The
  design keeps the hardest of those discriminations as explicit, named cases.
