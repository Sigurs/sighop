## ADDED Requirements

> Reference: DESIGN.md §5, §12 milestone 4 ("the first successful decrypt of a real MeshCore
> DM"). Every existing requirement of this capability stands unchanged; what follows adds the
> evidence that has been missing since milestone 1.

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
