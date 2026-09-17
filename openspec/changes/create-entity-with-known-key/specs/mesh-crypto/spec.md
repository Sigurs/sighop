## MODIFIED Requirements

### Requirement: Identity keys and node hash
The system SHALL represent an entity identity as an Ed25519 keypair and SHALL derive its node
hash as the first byte of the 32-byte public key. The private half of that keypair SHALL be held
in the 64-byte representation the firmware stores and exports — the SHA-512 expansion of a seed
with the standard clamping already applied to its first 32 bytes — and the public key SHALL be
derived from the first 32 bytes of that representation by unclamped base-point multiplication,
with no re-hashing and no re-clamping. The system SHALL NOT retain the seed a generated keypair
was expanded from: a seed is a way to produce a keypair, not a way to hold one.

#### Scenario: Node hash derivation
- **WHEN** a node hash is computed for a public key
- **THEN** it equals that key's first byte

#### Scenario: Keypair generation avoids a local hash collision
- **WHEN** a keypair is generated with a set of node hashes already in use by local entities
- **THEN** generation repeats until the new keypair's node hash is not in that set, and the returned keypair's hash is distinct from all of them

#### Scenario: Every node hash is already taken
- **WHEN** keypair generation is asked to avoid a set covering all 256 possible node hashes
- **THEN** it fails with a clear error after a bounded number of attempts rather than looping forever

#### Scenario: Public key derived from the stored private key
- **WHEN** a keypair is generated and its public key is derived again from the 64-byte private key it holds
- **THEN** the derived key equals the one the keypair was created with

#### Scenario: A generated keypair does not retain its seed
- **WHEN** a keypair is generated and inspected for the material it holds
- **THEN** it exposes the 64-byte private key and the public key, and exposes no seed

### Requirement: Advert signing and verification
The system SHALL sign an advert with Ed25519 over the concatenation of the 32-byte public key,
the 4-byte little-endian timestamp, and the appdata bytes, in that order, and SHALL verify an
inbound advert's signature over the same concatenation using the public key carried in the
advert itself. Signing SHALL be computed from the 64-byte private key alone — its clamped scalar
and its nonce prefix — so that an identity built from a private key supplied by an operator signs
exactly as a generated one does.

#### Scenario: Every corpus advert verifies
- **WHEN** the ADVERT packets in the capture corpus are verified
- **THEN** every one of them passes signature verification, confirming the signed-message construction

#### Scenario: Tampered advert appdata fails verification
- **WHEN** a single byte of a corpus advert's appdata or timestamp is altered and the advert is re-verified
- **THEN** verification fails

#### Scenario: Sign and verify round-trip
- **WHEN** an advert is signed with a generated identity and then verified
- **THEN** verification succeeds against that identity's public key

#### Scenario: An identity built from a supplied private key signs identically
- **WHEN** a message is signed by a generated identity and by an identity constructed from that same identity's 64-byte private key
- **THEN** the two signatures are byte-for-byte identical, and both verify against the public key

## ADDED Requirements

### Requirement: Signing is proven against a reference implementation
The system SHALL anchor its signing construction to a foreign implementation rather than to its
own output, because a signature computed from the expanded private key is only useful if every
other node accepts it. The anchor SHALL include the keypair embedded in the firmware's private
key validation, so a drift in clamping or in nonce derivation fails a test rather than producing
adverts no peer verifies.

#### Scenario: Signatures match a reference implementation
- **WHEN** a message is signed with a private key whose corresponding seed is known, and the same message is signed by a reference Ed25519 implementation given that seed
- **THEN** the two signatures are byte-for-byte identical

#### Scenario: The vector is anchored to its provenance
- **WHEN** the signing known-answer vector is read
- **THEN** it names the firmware source the keypair comes from, and its expected signature is a fixed literal rather than a value recomputed by the system under test
