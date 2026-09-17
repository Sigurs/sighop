## 1. The private key representation

- [x] 1.1 Add the known-answer signing vector to `tests/protocol/test_crypto.py` — the keypair from `Identity.cpp::validatePrivateKey`, its expanded private key and an expected signature as fixed literals, with the source named in a comment — and verify it fails against today's code only by being absent, not by disagreeing (write it before 1.2 so the new signer is checked against it rather than to it)
- [x] 1.2 Change `LocalIdentity` in `src/sighop/protocol/identity.py` to hold `private_key: bytes[64]` and `public_key`, keep `from_seed` as the expanding constructor, add `from_private_key`, make `private_scalar` a slice of the stored key, drop `meshcore_private_key`'s recomputation, and verify `tests/protocol/test_crypto.py` passes for every test that does not touch signing
- [x] 1.3 Implement `sign` as the scalar construction in design D2 and verify both the literal vector from 1.1 and a new property test asserting `from_seed(s).sign(m) == crypto_sign(m, s + pub)[:64]` over at least 20 seeds and varied message lengths, including the empty message
- [x] 1.4 Add `private_key_from_hex` and the reserved/collision refusals (design D6, D7) to `identity.py`, and verify unit tests cover non-hex input, 32-byte input, 63- and 65-byte input, an unclamped scalar in each of the three clamped bits, a key deriving `0x00`, one deriving `0xFF`, and one colliding with a given taken set
- [x] 1.5 Remove the claim in `identity.py` and `src/sighop/protocol/crypto.py` that a key without a recoverable seed cannot sign, replace it with what is now true, and verify `grep -rn "cannot sign\|but not sign" src/` returns nothing
- [x] 1.6 Verify the whole existing protocol suite and the corpus advert verification are green — `pytest tests/protocol/` — proving no wire-visible value moved

## 2. The keyfile format

- [x] 2.1 Make `keyfile_document` write version 2 with `private_key_hex`, make `identity_from_document` read version 2 only, and verify `grep -n "seed_hex" src/sighop/keystore.py` returns nothing but the version 1 refusal's own message
- [x] 2.2 Give version 1 its own refusal naming the file and saying the seed format is gone (design D3), and verify a test asserts that message rather than the generic unknown-version one, and that no identity is produced
- [x] 2.3 Verify `load_keyfile` still refuses an unknown version naming the file and the version, with a test at version 3, and that its message differs from 2.2's
- [x] 2.4 Verify the public key cross-check still refuses a version 2 document whose stored public key is not the one the private key derives
- [x] 2.5 Verify a version 2 keyfile round-trips: create it, export it, load the export, and assert the identity is unchanged and no file carries `seed_hex`
- [x] 2.6 Regenerate the committed burned test fixture keyfile as version 2 and verify the tests that load it pass, keeping one small version 1 document inline in the test file as 2.2's fixture rather than as a committed keyfile

## 3. The entity store

- [x] 3.1 Rename `seal_seed`/`open_seed` to `seal_private_key`/`open_private_key` in `src/sighop/db/sealing.py`, sealing 64 bytes only and refusing a 32-byte plaintext with its own message naming the removed format (design D4), and verify `tests/test_sealing.py` covers a 64-byte round trip, the 32-byte refusal, a wrong secret, an altered box and a plaintext of some third length
- [x] 3.2 Verify the three failure messages are distinct — removed format, wrong secret, altered row — with a test asserting no one of them matches the wording of another
- [x] 3.3 Make `repositories.store` seal the 64-byte private key and `_open` call `from_private_key`, and verify a test that seals a 32-byte payload directly into a row and then loads it, expecting the removed-format refusal naming the entity and the row left unchanged
- [x] 3.4 Verify a run whose store holds one stranded row and one good row loads the good identity and reports the stranded one by name, rather than failing the whole load
- [x] 3.5 Write `alembic/versions/0008_private_key.py` renaming `entity.sealed_seed` to `sealed_private_key` with a reversing downgrade, reporting a count of the rows that will stop opening (design D5), and verify `sighop db upgrade` then `downgrade` runs with no `SIGHOP_SECRET_KEY` in the environment
- [x] 3.6 Verify the migration's report carries a count and no entity name, public key or ciphertext, with a test asserting the output against a database seeded with rows
- [x] 3.7 Update `src/sighop/db/models.py` and verify `tests/test_entity_store.py` passes, including the test asserting the stored column is ciphertext and holds no plaintext key

## 4. The command line

- [x] 4.1 Add `--private-key` to `sighop keys new` (design D7) and verify a keyfile written from a supplied key has the derived public key, that `keys show` on it matches, and that no key was generated
- [x] 4.2 Verify `keys new --private-key` refuses each invalid key with the message `private_key_from_hex` raises, and that no file exists afterwards in each case
- [x] 4.3 Add `--private-key` with its required `--name` and optional `--node-type` to `sighop keys import`, mutually exclusive with the keyfile argument (design D8), and verify importing a supplied key stores the row, prints the derived public key and node hash, and writes no file
- [x] 4.4 Verify `keys import` refuses both inputs together and neither input at all, each naming them as alternatives, and that nothing is stored in either case
- [x] 4.5 Verify `keys import --private-key` refuses a key whose public key is already stored, naming the existing entity, and leaves the stored row unchanged
- [x] 4.6 Change every operator-facing "seed" in `src/sighop/cli.py` to "private key", including `PLAINTEXT_SEED_NOTICE`, and verify `tests/test_first_transmit.py` and `tests/test_entity_store.py` assert the new wording and that no command prints key material it did not before

## 5. The panel

- [x] 5.1 Add the optional private key field to the create form in `src/sighop/web/templates/admin/identities.html` and to `create_identity` in `src/sighop/web/routes/keys.py`, calling the same helpers the command line calls (design D6), and verify a browser-created identity from a key matches one the command line created from the same key — extend `tests/test_web_write_parity.py`
- [x] 5.2 Verify an empty key field still generates, and that a supplied key is refused with the command line's own words, re-rendering the page with the other typed fields preserved and nothing stored
- [x] 5.3 Verify the refusal re-render does not echo the submitted private key back into the form, and that no log line, refusal message or template context carries it
- [x] 5.4 Change the seed wording in `templates/admin/identities.html`, `identity.html`, `revealed.html` and `src/sighop/web/guarded.py`, pass `private_key=` from `web/routes/admin.py`'s reveal, and verify `tests/test_web_admin.py` and `tests/test_web_guard.py` assert the reveal page names a private key and still sits behind its guarded action

## 6. Whole-system verification

- [x] 6.1 Verify the full suite passes — `pytest` — with particular attention to `tests/test_dm.py`, `tests/test_adverts.py`, `tests/test_channels.py` and `tests/test_room_sync.py`, none of which should have needed a change
- [x] 6.2 Verify a round trip an operator would do: `keys new` a keyfile, `keys import` it, `keys export` it back, re-create from the exported private key with `keys new --private-key`, and assert one public key throughout
- [x] 6.3 Verify interoperability end to end against a real peer — bring in an identity from a device by its private key, advert from it, and confirm the peer accepts the advert signature and can exchange a direct message with it
- [x] 6.4 Update `DESIGN.md` §3 and §5 where they describe the seed as the private material, naming the representation this change settles on and why, and record that the old format is refused rather than converted
- [x] 6.5 Write the upgrade note an operator follows — design's Migration Plan, including the out-of-sighop seed expansion for an identity worth carrying forward — into the project's deployment documentation, and verify it names the step that cannot be done after upgrading

## 7. Removing a stored identity (design D10)

Found while running task 6.2: the removed-format refusal tells an operator to re-import the
identity, and `store()` refuses that because the stranded row still holds the public key. Nothing
in `sighop keys` could remove a stored identity, so the recovery this change documents needed
`psql`.

- [x] 7.1 Add `EntityRepository.remove(public_key)` returning whether a row went, and verify a test that removes one of two stored identities and leaves the other
- [x] 7.2 Add a bindings lookup usable outside the panel — what rooms and bots are bound to an entity id — and verify it reports a bound room and a bound bot and is empty for an unbound identity
- [x] 7.3 Add `sighop keys delete <reference>` selecting an identity exactly as `keys export` does, and verify it removes an unbound identity on a matching confirmation and prints the name and public key
- [x] 7.4 Verify it refuses an identity a room or a bot is bound to, naming what it serves, and that the row and the bound room or bot are all unchanged — the foreign keys cascade, so this is the test that stops a room's history going with it
- [x] 7.5 Verify it refuses with no terminal and no `--delete-key`, stating the cost, and refuses a confirmation that is not the identity's name, removing nothing in both cases
- [x] 7.6 Verify the consequence line distinguishes an openable row from a stranded one, and that removal still works when no sealing secret is configured
- [x] 7.7 Verify a reference matching several identities is refused listing what matched, reusing `keys export`'s own matching
- [x] 7.8 Verify the recovery end to end: strand a row, `keys delete` it, `keys import --private-key` the same key, and assert the public key and node hash are what they were
- [x] 7.9 Name the absence in the panel where an operator would look for it — identity administration — stating that removal is a terminal command and that disabling is the reversible action offered there, and verify `tests/test_web_admin.py` asserts it
