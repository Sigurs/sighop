# Spec Delta

## ADDED Requirements

### Requirement: Decryption is exercised end to end over the synthetic corpus
The system SHALL carry tests that decrypt the synthetic corpus's encrypted frames — direct messages,
group text on the Public channel and group text on a second synthetic channel — with the keys the
generator holds, and SHALL assert that the shared-secret derivation, the cipher and the MAC together
recover the generator's known plaintext. These tests SHALL run in the ordinary test suite.

#### Scenario: Corpus direct messages decrypt
- **WHEN** the synthetic corpus's direct messages are decrypted with the recipient's key and the sender's public key
- **THEN** every MAC verifies and every plaintext parses as a text message body equal to the generator's message

#### Scenario: Corpus Public frames decrypt
- **WHEN** every distinct `GRP_TXT` frame on channel hash `0x11` in the synthetic corpus is verified and decrypted under the Public channel key
- **THEN** every MAC verifies and every plaintext parses as plain group text whose sender and text are from the synthetic cast

#### Scenario: The vectors name their corpus and counts
- **WHEN** the known-answer tests are read
- **THEN** they name the corpus files and the counts of frames they expect, so a corpus change that alters a count fails the test rather than silently narrowing it

#### Scenario: The two key slices stay distinguishable
- **WHEN** a corpus direct message is decrypted with the full 32-byte secret as the cipher key, or MAC'd with only its first 16 bytes
- **THEN** the test fails, so that the two distinct key slices stay distinguishable by evidence rather than by comment

#### Scenario: The channel hash is taken over the key at its real length
- **WHEN** the Public channel hash is computed over the zero-extended 32-byte buffer instead of the 16-byte key
- **THEN** it is `0x17`, not the `0x11` every Public frame in the corpus carries

#### Scenario: Frames on another channel stay closed
- **WHEN** corpus `GRP_TXT` frames on a channel hash other than `0x11` are trialled under the Public key
- **THEN** none decrypts, and all decrypt under the second synthetic channel's key

### Requirement: Acknowledgement checksums are exercised over the synthetic corpus
The system SHALL assert its acknowledgement construction over the synthetic corpus, for both the
4-byte and the 6-byte acknowledgement forms, against the messages the acknowledgements answer.

#### Scenario: A corpus acknowledgement matches the message it answers
- **WHEN** a corpus acknowledgement is compared with the checksum computed for the message it answers
- **THEN** the first four bytes match

### Requirement: The limits of the crypto evidence are stated
The system SHALL state, wherever the corpus documents what it proves about cryptography, that the
synthetic corpus is produced by sighop's own encryptor and therefore establishes agreement between
sighop's encryptor and decryptor and the recorded expectations, and does not establish
interoperability with any other MeshCore implementation. It SHALL NOT present a green corpus run as
evidence of interoperability.

#### Scenario: The documentation says what is not proven
- **WHEN** the corpus documentation's cryptography section is read
- **THEN** it says that interoperability is anchored only by the firmware-embedded signing keypair and the fixed known-answer vectors transcribed from the firmware source, and that a recorded exchange with a stock implementation existed once and was withdrawn with the recorded corpus

## REMOVED Requirements

### Requirement: Decryption is proven against a foreign implementation
**Reason**: The ciphertext was a direct message produced by stock MeshCore firmware between the
operator's dev boards, opened with a committed private key. Keeping it means keeping a real private
key, a real public key and a device name in the repository.
**Migration**: Replaced by "Decryption is exercised end to end over the synthetic corpus", which is a
self-consistency test. The interoperability claim is withdrawn rather than transferred; it remains in
git history and in DESIGN.md as a finding.

### Requirement: Acknowledgement checksums are proven against a foreign implementation
**Reason**: The acknowledgements were recorded from the same real exchange as the direct messages and
cannot be kept without it.
**Migration**: Replaced by "Acknowledgement checksums are exercised over the synthetic corpus".

### Requirement: Channel decryption is proven against a foreign implementation
**Reason**: The 85 Public-channel frames were other people's recorded chat. The Public key is public,
so keeping the frames means keeping their plaintext.
**Migration**: Replaced by the Public-channel scenarios of "Decryption is exercised end to end over
the synthetic corpus", over generated frames under the same key, with the `0x11` / `0x17` channel hash
discrimination and the closed-channel negative preserved.
