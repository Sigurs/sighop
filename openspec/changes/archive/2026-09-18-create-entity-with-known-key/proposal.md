## Why

An operator who already holds a MeshCore identity — one generated on a device, or kept in a
password manager from a previous install — cannot bring it into sighop. Every creation path
generates a fresh keypair, and the only import path takes a sighop keyfile, which no other
implementation writes.

The obstacle is a representation choice. `LocalIdentity` holds the 32-byte seed as its canonical
private material, and MeshCore's exported private key is the 64-byte SHA-512 expansion of a seed
that cannot be run backwards. `identity.py` records the consequence as a hard one: a foreign
64-byte key "can derive shared secrets and verify but not sign".

That consequence is wrong. MeshCore's `prv_key` is the full Ed25519 expansion — the clamped
scalar in bytes `[0:32]`, the signing nonce prefix in bytes `[32:64]` — which is everything
RFC 8032 signing consumes. A signature computed from it is bit-identical to libsodium's, verified
against `crypto_sign` over 50 random keypairs. The seed's remaining advantages are that
`crypto_sign` accepts it directly and that it is half the bytes; against those, it cannot
represent an imported key at all. So the seed goes, and the 64-byte private key MeshCore stores
becomes the one representation sighop holds.

## What Changes

- **BREAKING (format).** `LocalIdentity` holds a 64-byte `private_key` and no seed. Generation
  still rolls 32 random bytes, expands them, and discards the seed — the seed is a way to make a
  keypair, not a way to hold one.
- `LocalIdentity.sign` is implemented over the expanded key with Ed25519 scalar arithmetic
  instead of `crypto_sign`, anchored by known-answer vectors against libsodium and against the
  keypair already embedded in `Identity.cpp::validatePrivateKey`.
- **BREAKING.** Keyfile version 2 records `private_key_hex` (128 hex characters) in place of
  `seed_hex`, and version 1 is no longer read. A version 1 keyfile is refused, naming the file and
  saying the format recorded a seed and is gone. There is no conversion command.
- **BREAKING.** The sealed database column holds the 64-byte private key, and a row sealed under
  the old format no longer opens. It is refused by the same wording rather than reported as a
  corrupt row. There is no re-encrypting migration and no conversion command: the migration
  renames `sealed_seed` → `sealed_private_key` and reports how many rows will stop opening.
- An identity held only in the old format is recovered by exporting it **before** upgrading and
  re-importing it after, or by recreating it. This is stated where the operator meets it.
- `sighop keys new --private-key <hex>` writes a keyfile for a private key the operator supplies
  instead of generating one.
- `sighop keys import --private-key <hex>` seals a supplied private key straight into the store,
  with no plaintext keyfile ever written to disk. There is no `--seed` flag on any surface.
- The panel's create-identity form gains an optional private key field, so the browser creates
  from a known key exactly as the command line does.
- A supplied key is refused, with no entity created, when it is not 64 bytes, when its scalar is
  not already clamped, when the public key it derives carries a reserved `0x00`/`0xFF` node hash,
  or when that node hash collides with a local entity the run already holds.
- Every operator-facing mention of "seed" becomes "private key" — command output, panel copy, the
  guarded reveal and export pages.

## Capabilities

### New Capabilities

None. This extends identity handling that already exists.

### Modified Capabilities

- `mesh-crypto`: the canonical private key representation becomes MeshCore's 64-byte `prv_key`;
  signing is specified over that key rather than over a seed, and an identity built from a
  supplied key signs identically to a generated one.
- `local-identity`: keyfile version 2 and its private key field, the refusal of version 1, and
  creating an identity from a private key the operator already holds, including what is refused.
- `entity-store`: the sealed column holds the private key, a row sealed under the old format is
  refused by name, and an identity can be imported from a supplied private key without a keyfile.
- `runtime-cli`: `keys new` and `keys import` accept `--private-key`, and what each refuses.
- `web-admin`: the create form accepts a private key, and refuses one the command line refuses in
  the same words.

## Impact

- `src/sighop/protocol/identity.py` — `LocalIdentity` representation, `sign`, `from_private_key`,
  validation of a supplied key; `generate_identity` unchanged in behaviour.
- `src/sighop/protocol/crypto.py` — `private_scalar` now a slice of the stored key; the docstring
  claiming a seedless key cannot sign is removed.
- `src/sighop/keystore.py` — keyfile version 2, `identity_from_document` reading it alone and
  refusing version 1 by name, `create_keyfile` accepting a supplied identity (already does).
- `src/sighop/db/sealing.py` — seal/open take 64 bytes and refuse a 32-byte payload by name.
- `src/sighop/db/repositories.py` — `store` seals the private key; `_open` takes 64 bytes.
- `src/sighop/db/models.py` + one Alembic migration — column rename, plus a count of the rows
  that will stop opening; no key material read.
- `src/sighop/cli.py` — `--private-key` on `keys new` and `keys import`, refusal messages, output
  wording.
- `src/sighop/web/routes/keys.py`, `templates/admin/identities.html`, `identity.html`,
  `revealed.html`, `web/guarded.py` — the create form field and the seed→private key wording.
- Tests: `tests/test_keystore.py`, `tests/test_sealing.py`, `tests/test_entity_store.py`,
  `tests/protocol/test_crypto.py`, `tests/test_first_transmit.py`, `tests/test_web_write_parity.py`,
  and the fixtures in `tests/dbfixtures.py`, `tests/webfixtures.py`.
- No wire-format change: the public key, node hash, advert signatures and shared secrets are
  byte-for-byte what they are today.
