## ADDED Requirements

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
