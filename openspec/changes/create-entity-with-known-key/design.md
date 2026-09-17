## Context

See proposal.md — Why, for the motivation and for the evidence that signing from the expanded
key is bit-identical to libsodium's.

What the code holds today:

- `LocalIdentity` (`src/sighop/protocol/identity.py`) is `(seed: bytes[32], public_key: bytes[32])`.
  `sign` calls `crypto_sign(msg, seed + public_key)`; `meshcore_private_key` expands the seed on
  demand; `private_scalar` is the first 32 bytes of that expansion and is what
  `crypto.py::shared_secret_from_scalar` multiplies with.
- There are exactly four construction sites: `generate_identity`, `keystore.identity_from_document`,
  `repositories._open`, and test fixtures. Everything else receives a `LocalIdentity`.
- The keyfile is JSON at version 1 with `seed_hex` and `public_key_hex`, written by
  `keyfile_document` and checked by `identity_from_document`, which refuses a stored public key
  the seed does not derive.
- `db/sealing.py` seals exactly `SEED_SIZE` bytes under a `SecretBox`, prefixed with
  `SEAL_VERSION = 1`. `entity.sealed_seed` holds the result. Migrations run to `0007_channels`.
- PyNaCl 1.6.2 is already a dependency and its build exposes `crypto_core_ed25519_scalar_*` and
  `crypto_scalarmult_ed25519_base_noclamp` (`has_crypto_core_ed25519` is true). No new dependency.

## Goals / Non-Goals

**Goals:**

- One private key representation in the process, at rest, and in the interchange format.
- A supplied key is refused before anything is written, on every surface, in the same words.
- Material in the old format is refused in words that say so, distinguishable from corruption and
  from a wrong secret, so an operator knows which of the three they are looking at.

**Non-Goals:**

- Changing anything on the wire. Public keys, node hashes, advert signatures, shared secrets and
  channel keys are byte-for-byte what they are today; the corpus tests are the proof.
- Reading the old format anywhere, or any tooling that converts it. Operator decision, taken
  during planning: seeds are not supported and are not converted.
- Re-encrypting stored rows, or any migration that needs `SIGHOP_SECRET_KEY`.
- Importing a MeshCore *keyfile* or device backup format. This change takes a hex private key,
  which is what those tools display and what a password manager holds.
- Changing who may see key material. The guarded reveal and export stay guarded actions.

## Decisions

### D1 — The 64-byte private key is canonical; the seed is dropped, not made optional

`LocalIdentity` becomes `(private_key: bytes[64], public_key: bytes[32])`. `from_seed(seed)`
stays, as the constructor `generate_identity` uses: it expands and discards. `from_private_key`
is the new one. `meshcore_private_key` becomes the stored field and `private_scalar` a slice of
it, so `crypto.py` is unchanged in behaviour.

*Alternative considered:* `seed: bytes | None` alongside the private key, keeping `crypto_sign`
for generated identities. Rejected — two representations means every consumer needs a `None`
branch, the keyfile has two shapes, and signing takes two code paths of which only one is
exercised by the corpus tests. The path that matters least would be the one under test.

*Cost accepted:* we own an Ed25519 signing implementation. D2 is the mitigation.

### D2 — Signing is scalar arithmetic on PyNaCl bindings, anchored by known-answer vectors

```
a, prefix = prv[:32], prv[32:]
r = scalar_reduce(sha512(prefix + msg))
R = base_noclamp(r)
k = scalar_reduce(sha512(R + pub + msg))
S = scalar_add(r, scalar_mul(k, a))
signature = R + S
```

Six lines, no field arithmetic of our own, no re-clamping. Verified against `crypto_sign` over
50 random keypairs during planning.

The tests anchor it two ways: a property test that signing via `from_seed(s)` equals
`crypto_sign(msg, s + pub)` for a set of seeds, and a fixed literal vector built on the keypair
already embedded in `Identity.cpp::validatePrivateKey` — the same provenance rule
`tests/protocol/test_crypto.py` follows for the decryption vectors. A literal, because a vector
the system under test recomputes proves nothing.

*Alternative considered:* recover a seed to keep using `crypto_sign`. Impossible — SHA-512 is not
invertible, which is the whole reason for this change.

### D3 — Keyfile version 2 writes `private_key_hex`; version 1 is refused, not converted

`keyfile_document` writes `version: 2` and `private_key_hex`. `identity_from_document` reads
version 2 and nothing else. `seed_hex` is not read at any version, and `from_seed` is not reachable
from the keyfile path.

Version 1 gets its own refusal rather than falling through the existing unknown-version message:
it is a version we *did* write, and an operator meeting it deserves "this format recorded a seed
and is gone; re-create the identity from its private key, or from an export taken before the
upgrade" rather than "version 1 is not the supported version 2", which reads like a corrupt file.

No conversion command. Operator decision during planning: a keyfile is material the system does
not own, and the codebase's own rule that `keys export` is "never a side effect" cuts against a
read that rewrites a file.

*Alternative considered:* dual read, or `sighop keys convert`. Both rejected by the operator — see
Risks for what that costs.

### D4 — The sealed value is 64 bytes; a 32-byte one is refused by name

`seal_seed`/`open_seed` become `seal_private_key`/`open_private_key`. Sealing takes 64 bytes and
nothing else. Opening still *inspects* the plaintext length — not to support the old format but
to name it: a 32-byte plaintext raises a distinct error saying the row holds a seed under the
removed format, the row is intact, and the identity must be re-imported. Without that check the
operator gets "the sealed value opened to 32 bytes, not a 64-byte private key; the row is
corrupt", which is false and sends them looking for the wrong problem.

So `SEAL_VERSION` stays 1: the envelope did not change, and the three failures an operator can
hit — wrong secret, altered row, removed format — stay three distinct messages.

`repositories._open` calls `from_private_key` and runs the existing public key comparison.

*Consequence:* a row under the old format is a dead row until it is deleted or overwritten. It
refuses loudly every time it is loaded, which is the intent.

### D5 — The migration renames the column and counts what it breaks

`entity.sealed_seed` → `entity.sealed_private_key`, as `alembic/versions/0008_private_key.py`,
an `op.alter_column` with a downgrade that reverses it. It reads no key material, so it runs
without the secret.

It also counts the rows it is about to strand — `SELECT count(*) FROM entity`, every one of which
is under the old format at the moment the migration runs — and reports that count. A count only:
naming the entities would put identity names in migration output that ends up in deploy logs, and
the operator can get the names from `sighop keys list`, which still works because the row is
readable as a row; it is only the key material that will not open.

Reporting it at all is the point. The alternative is an operator discovering it at the next start,
when the run refuses the identity it was about to advert from.

### D6 — Validation lives in one function, called by all three surfaces

`identity.py` gains `private_key_from_hex(text) -> LocalIdentity`, which is the single place that
rejects bad hex, a wrong length, and an unclamped scalar, and one `refuse_reserved_or_taken`
check for the node hash rules. The CLI and the panel both call them and print what they raise, so
the panel's refusal is the command line's refusal by construction rather than by a matching pair
of strings. This is the same shape `keyfile_from_text` already established for the import path.

Clamping is checked, never applied: `prv[0] & 0b111 == 0`, `prv[31] & 0b0100_0000 != 0`,
`prv[31] & 0b1000_0000 == 0`. An unclamped key that we clamped would derive a *different* public
key from the one the operator's other device advertises — a silently different identity is worse
than a refusal.

### D7 — The reserved and collision checks apply to a supplied key exactly as to a generated one

`generate_identity` skips `0x00`/`0xFF` and the taken set by retrying. A supplied key cannot
retry, so the same two conditions become refusals. Wiring:

- `sighop keys new --private-key` — reserved check, plus `avoid_node_hashes` if the existing
  keyfile-collision machinery is in play (`create_keyfile` already raises
  `NodeHashCollisionError` for a supplied identity).
- `sighop keys import --private-key` — reserved check; the store's public key uniqueness already
  refuses a duplicate identity, and the node hash set comes from the stored entities.
- Panel create — reserved check plus `_taken_hashes(page)`, the loaded *and* stored set the
  generated path already avoids.

### D8 — `keys import` takes a keyfile or a private key, never both, and a name with the key

A keyfile carries a name and a node type; a bare hex key carries neither. So `--private-key`
requires `--name` and takes `--node-type` with the same default `keys new` uses. The two input
modes are mutually exclusive and one is required — argparse says so, and the error names them as
alternatives rather than as a missing argument.

### D9 — Wording changes to "private key" wherever an operator reads it

Command output, panel copy on the identities page, the guarded reveal (`revealed.html`) and
export confirmations, and `guarded.py`'s consequence sentences. `web/routes/admin.py`'s reveal
passes `private_key=` in place of `seed=`. This is not cosmetic: the reveal page tells an
operator what the material they are looking at *is*, and after this change it is a 128-character
private key, not a 64-character seed.

### D10 — `sighop keys delete`, because a stranded row blocks its own recovery

Found by exercising the migration path rather than by reading it: the refusal in D4 tells an
operator to re-import the identity, and `store()` refuses that, because the stranded row still
holds the public key. There has never been a way to remove a stored identity — the `keys` surface
is new/show/secret/list/import/export — so the recovery this change documents was impossible
without `psql`.

`sighop keys delete <reference>` selects an identity exactly as `keys export` does, so the
matching and the ambiguity refusal are one behaviour rather than two.

**It refuses an identity a room or a bot is bound to.** `room.entity_id` and `bot.entity_id` are
`ON DELETE CASCADE`, so removing an identity silently takes a room's members and its whole
message history with it. Nobody types `keys delete` meaning that. The refusal names what the
identity serves, and the operator removes the room or the bot first — which are their own
commands, with their own consequence messages.

**It confirms the way `_channel_remove` confirms**, because that pattern is already in this CLI
and an operator should not meet a second one: state the consequence, ask for the identity's name
at a terminal, and accept `--delete-key` instead where there is no terminal. Refusing to act
without a terminal *or* the flag is what stops a script deleting an identity by being run twice.

**The consequence line says which case this is.** If the row opens, it says the identity is
unrecoverable unless its private key is held elsewhere. If the row is stranded, it says the key
material could not be read anyway, so nothing usable is lost — which is the whole reason the
operator is there. Classifying needs the sealing secret; without one, the message says it cannot
tell, and removal still works, because a row nobody can open is exactly what an operator with no
secret may need to clear.

*Panel parity is deliberately not offered.* `web-admin` already has a requirement for naming
capabilities the browser does not expose, and this joins migrations, secret generation and
account management there. Disabling is the reversible action the panel offers; an irreversible
one belongs where `keys secret` already lives.

## Risks / Trade-offs

- **We now own a signing implementation.** A mistake produces adverts no peer verifies — the
  loudest possible failure, but only at runtime. → D2's literal known-answer vector, the
  libsodium equivalence property test, and the existing corpus advert verification, which is
  unchanged and must stay green.
- **Every identity stored or kept as a keyfile before this change stops working.** This is the
  operator's explicit decision, not an oversight, and it is the largest thing in this change. →
  The recovery is to export before upgrading; the migration's count and the load-time refusals
  both say what happened. Nothing is destroyed: the rows and files remain, they just no longer
  open, so an operator who upgrades first can still roll back and export.
- **A stranded row or keyfile reads as corruption if the message is wrong.** An operator told
  "corrupt" will go looking for a disk or a database problem that does not exist. → D3 and D4
  each give the removed format its own message, and a spec scenario requires it to be
  distinguishable from an altered row and from a wrong secret.
- **An operator pasting a private key into a browser form.** The panel's create form now accepts
  key material over HTTP. → The panel already reveals and exports key material behind guarded
  actions and its own auth; the field is optional, the value is never logged (it goes to
  `store()` and to the refusal path, and the refusal re-render must not echo it back into the
  form the way the other fields are preserved), and the page states what it is.
- **The format break runs in both directions.** An older build reads a version 2 keyfile as
  unknown and a 64-byte sealed payload as corrupt, exactly as the new build refuses the old ones.
  → The migration's downgrade reverses the column rename and nothing else. Stated in the
  migration plan rather than mitigated.
- **`sealed_seed` appears in operator notes and dashboards.** → The rename is a single DDL step
  with a downgrade; `openspec/specs/database` and the schema-revision page report the revision,
  so the change is visible where the schema is.

## Migration Plan

This is a one-way format break in both directions, so the order matters.

1. **Before upgrading**, on the current build: `sighop keys export` every stored identity worth
   keeping. This is the only step that cannot be done afterwards, and what it saves is a seed —
   which the new build will not read. Saving it anyway is what makes step 3 possible.
2. Deploy applies `0008_private_key` (column rename, no key material read, no secret needed). It
   reports how many rows will stop opening — expect that to be every row that existed.
3. For each identity to carry forward, turn its saved seed into a private key **outside sighop** —
   `sha512(seed)`, then `[0] &= 248; [31] &= 63; [31] |= 64` — and feed the 128 hex characters to
   `sighop keys import --private-key`. The public key and node hash come out unchanged, so no
   peer has to be told anything and no contact list needs editing.

   Six lines in any language, and deliberately not a command: an expansion the operator performs
   once, knowingly, on material they already hold is a different thing from the system reading
   seeds. An identity whose seed was not saved in step 1 cannot be carried forward and has to be
   recreated, which does change its public key and does mean every peer re-adds it.
4. Delete or overwrite the stranded rows once the identities are back; they refuse loudly on every
   load until then, which is the reminder.
5. Rollback: `alembic downgrade` reverses the rename. Identities *created after* the upgrade are
   not readable by the previous build, and identities stranded by step 2 become readable again —
   so a rollback taken before step 3 loses nothing. After step 3 it is a roll-forward or a restore.
