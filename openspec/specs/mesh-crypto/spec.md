# mesh-crypto Specification

## Purpose
The cryptography MeshCore actually uses on the wire — identity keys and node hashes, shared
secret derivation, cipher, MAC, channel keys, advert signing — and, just as importantly, the
limit of what each one proves. The layer is pure and offline: it decides nothing about the mesh.
## Requirements

> Reference: `related-repos/MeshCore/src/Utils.cpp`, `src/Identity.cpp`,
> `src/helpers/BaseChatMesh.cpp` and `src/MeshCore.h` are authoritative here — the published
> payload documentation does not describe the cryptography. Where this spec and DESIGN.md §5
> disagree, the firmware source is what interoperates.
>
> These constructions are weak by modern standards (ECB, no nonce, a 2-byte MAC). Reproducing
> them exactly is a hard interoperability requirement, not an endorsement; the requirements
> below therefore also constrain how much trust the rest of the system may place in them.

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

### Requirement: Shared secret derivation
The system SHALL derive the shared secret between a local entity and a peer by converting the
peer's Ed25519 public key to its Montgomery (X25519) form and performing scalar multiplication
with the local entity's private scalar, producing a 32-byte secret that matches what MeshCore's
`LocalIdentity::calcSharedSecret` produces for the same key pair.

#### Scenario: Both parties derive the same secret
- **WHEN** two generated identities each derive a shared secret against the other's public key
- **THEN** both derive byte-identical 32-byte secrets

#### Scenario: Derivation matches a fixed known-answer vector
- **WHEN** the shared secret is derived for a recorded keypair-and-peer-key test vector
- **THEN** it equals the recorded expected secret, proving the conversion matches the firmware rather than merely being self-consistent

#### Scenario: Repeated derivation for the same peer is cached
- **WHEN** a shared secret is requested more than once for the same local entity and peer key
- **THEN** the scalar multiplication is performed once and the cached secret is returned thereafter

### Requirement: Cipher
The system SHALL encrypt and decrypt with AES-128-ECB, keyed on the **first 16 bytes** of the
32-byte shared secret, zero-padding plaintext up to a 16-byte boundary — never PKCS#7 — and
SHALL treat ciphertext whose length is not a positive multiple of 16 as invalid.

#### Scenario: Encrypt then decrypt round-trip
- **WHEN** a plaintext of arbitrary length is encrypted and then decrypted with the same shared secret
- **THEN** the decrypted output begins with the original plaintext, padded with zero bytes to the next 16-byte boundary

#### Scenario: Plaintext exactly fills a block
- **WHEN** a 16-byte plaintext is encrypted
- **THEN** the ciphertext is exactly 16 bytes, with no additional padding block appended

#### Scenario: Plaintext length is not recoverable from the ciphertext
- **WHEN** a decrypted buffer is returned
- **THEN** the cipher layer reports the padded buffer as-is and does not attempt to guess the original length, leaving trailing-zero handling to the payload layer that knows the body's structure

### Requirement: Message authentication code
The system SHALL compute the cipher MAC as HMAC-SHA256 over the **ciphertext**, keyed on the
**full 32-byte shared secret** — not the 16-byte cipher key — truncated to its first 2 bytes,
and SHALL compare MACs in constant time.

#### Scenario: MAC verifies for a correctly keyed ciphertext
- **WHEN** a payload is encrypted-then-MAC'd and the MAC is verified with the same shared secret
- **THEN** verification succeeds and the ciphertext is decrypted

#### Scenario: MAC fails for a wrong key
- **WHEN** a MAC is verified against a ciphertext using a different shared secret
- **THEN** verification fails and no decryption is attempted

#### Scenario: MAC comparison is constant time
- **WHEN** two MAC values are compared
- **THEN** the comparison uses a constant-time primitive rather than byte-wise short-circuiting equality

### Requirement: A MAC match is not proof of identity
The system SHALL expose MAC verification as a candidate-match result rather than an
authentication result, and its API SHALL make clear that with a 1-byte destination hash and a
2-byte MAC a false match is expected at roughly 1 in 2^16 per candidate key.

#### Scenario: Multiple local entities match one destination hash
- **WHEN** a payload's destination hash matches more than one local entity
- **THEN** each candidate entity attempts MAC verification independently, and a success is reported as "this key decrypts this payload" without being treated as a security-critical proof of the sender's identity

### Requirement: Channel keys
The system SHALL support channel keys supplied as a 16-byte or 32-byte pre-shared key, and
SHALL derive a key from a hashtag name as the first 16 bytes of `sha256` over the hashtag
string. The channel hash carried in a group payload SHALL be the first byte of `sha256` over
the channel key.

#### Scenario: Hashtag-derived key
- **WHEN** a channel key is derived from the hashtag `#roomname`
- **THEN** it equals the first 16 bytes of the SHA-256 digest of the bytes `#roomname`

#### Scenario: Channel hash from a key
- **WHEN** a channel hash is computed for a channel key
- **THEN** it equals the first byte of the SHA-256 digest of that key

#### Scenario: Hashtag-derived keys are flagged as weak
- **WHEN** a channel key is derived from a hashtag rather than supplied as a pre-shared key
- **THEN** the returned channel carries a flag marking its key as brute-forceable, so consumers can surface that to an operator

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

### Requirement: Unverified adverts are discarded
The system SHALL expose advert parsing such that no advert content — name, node type, location
or public key — is available to a consumer without an accompanying signature verification
result, and the documented contract SHALL be that a failed or absent verification is a
discard, not a warning.

#### Scenario: Advert with an invalid signature
- **WHEN** an advert fails signature verification
- **THEN** the result reports the failure and its content is marked unverified, so a consumer cannot use the parsed name without having observed the verification outcome

### Requirement: Advert timestamp replay detection input
The system SHALL expose the advert's emitted timestamp on the verified result so that a
consumer can reject an advert whose timestamp is not newer than the last one seen from the
same identity, matching the firmware's replay check.

#### Scenario: Verified advert exposes its timestamp
- **WHEN** an advert is verified
- **THEN** the result carries the advert's emitted unix timestamp alongside the public key, so replay comparison is possible at the layer that stores contacts

### Requirement: Acknowledgement checksum
The system SHALL compute an acknowledgement checksum as the **first 4 bytes of SHA-256** over
the decrypted message body prefix (4-byte timestamp, the `txt_type`/attempt byte, and the
message text) concatenated with the **sender's** 32-byte public key. It SHALL NOT use CRC32.

#### Scenario: Checksum matches the firmware construction
- **WHEN** an acknowledgement checksum is computed for a recorded message body and sender public key
- **THEN** it equals the first four bytes of the SHA-256 digest of the body prefix concatenated with that public key

#### Scenario: Sender and receiver compute the same expected acknowledgement
- **WHEN** a message is built and its expected acknowledgement is computed by the sender, and the same message is parsed and acknowledged by the recipient
- **THEN** both compute the same 4-byte value

### Requirement: An acknowledgement is not authentication
The system SHALL document and expose acknowledgement checking as delivery evidence only, with
no claim of sender authentication, since the value is an unkeyed truncated hash over data any
observer of the plaintext could reproduce.

#### Scenario: Acknowledgement match reported as delivery evidence
- **WHEN** an inbound acknowledgement's checksum matches an outstanding outbound message
- **THEN** the result reports delivery evidence for that message and does not assert the identity of whoever sent the acknowledgement

### Requirement: Cryptography layer is pure and offline
The system SHALL implement all cryptography in Python over `bytes`, with no use of the
modem's `SetHardware` crypto sub-commands, and with no dependency on the radio, database or
network layers.

#### Scenario: Crypto module used without hardware
- **WHEN** the crypto module is imported and exercised in a process with no serial device attached
- **THEN** all operations complete, since no operation delegates to the modem's hardware identity

### Requirement: Decryption is proven against a foreign implementation
The system SHALL carry a known-answer test whose ciphertext was produced by a MeshCore
implementation other than sighop, encrypted to a key sighop holds, and SHALL assert that the
shared-secret derivation, the cipher and the MAC together recover the expected plaintext. This
test SHALL run in the ordinary test suite, not only in a live exercise.

#### Scenario: Recorded peer ciphertext decrypts
- **WHEN** the recorded direct message from the reference peer is decrypted with the recorded recipient identity's key
- **THEN** the MAC verifies, the plaintext recovers, and it parses as a text message body with the expected timestamp and text

#### Scenario: The vector is anchored to its provenance
- **WHEN** the known-answer test is read
- **THEN** it names the capture file, the frame within it, the peer firmware version that produced the ciphertext, and the burned identity keyfile that opens it

#### Scenario: The vector fails under a wrong key slice
- **WHEN** the same ciphertext is decrypted with the full 32-byte secret as the cipher key, or MAC'd with only its first 16 bytes
- **THEN** the test fails, so that the two distinct key slices stay distinguishable by evidence rather than by comment

### Requirement: Acknowledgement checksums are proven against a foreign implementation
The system SHALL assert its acknowledgement construction against an acknowledgement produced by
another MeshCore implementation for a message sighop sent, and against an acknowledgement
sighop produced that the peer accepted.

#### Scenario: The peer's acknowledgement matches our expectation
- **WHEN** the recorded acknowledgement for a message sighop transmitted is compared against the checksum sighop computed for that message
- **THEN** the first four bytes match

### Requirement: Channel decryption is proven against a foreign implementation
The system SHALL carry a known-answer test over group text frames produced by MeshCore nodes other
than sighop, recorded in the capture corpus, and SHALL assert that the stock Public channel key's
channel hash, MAC key and cipher key together verify and decrypt every distinct `GRP_TXT` frame on
that channel's hash. This test SHALL run in the ordinary test suite.

#### Scenario: Corpus Public frames decrypt
- **WHEN** every distinct `GRP_TXT` frame on channel hash `0x11` in the capture corpus is verified and decrypted under the Public channel key
- **THEN** every MAC verifies and every plaintext parses as plain group text with a claimed sender name

#### Scenario: The vector is anchored to its provenance
- **WHEN** the known-answer test is read
- **THEN** it names the capture files and the count of frames it expects, so a corpus change that alters the count fails the test rather than silently narrowing it

#### Scenario: The channel hash is taken over the key at its real length
- **WHEN** the Public channel hash is computed over the zero-extended 32-byte buffer instead of the 16-byte key
- **THEN** it is `0x17`, not the `0x11` every corpus frame carries, so the length the hash is taken over stays distinguishable by evidence rather than by comment

#### Scenario: Frames on another channel stay closed
- **WHEN** corpus `GRP_TXT` frames on a channel hash other than `0x11` are trialled under the Public key
- **THEN** none decrypts

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
