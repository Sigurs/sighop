# Spec Delta

## MODIFIED Requirements

### Requirement: Corpus coverage is recorded, including its gaps
The system SHALL record, alongside the corpus, which packet and payload shapes it does and
does not exercise, so that synthetic tests are written deliberately for the gaps rather than
coverage being assumed, and SHALL assert a shape the corpus has since acquired against the
recorded frames rather than leaving it to a synthetic fixture alone.

#### Scenario: Gaps are covered by synthetic fixtures
- **WHEN** a shape absent from the corpus is identified — `ROUTE_TYPE_TRANSPORT_DIRECT` packets, MULTIPART, RAW_CUSTOM, reserved payload types, CONTROL subtypes other than node discovery, the 14-byte key-prefix discovery response, hop counts above 5, and the reserved 4-byte hash size code
- **THEN** a synthetic fixture exists for it in the test suite, and the corpus documentation states that the corpus itself does not cover it

#### Scenario: The corpus acquires a shape that was previously synthetic-only
- **WHEN** a live capture appended to the corpus contains a `ROUTE_TYPE_TRANSPORT_FLOOD` frame or a CONTROL payload
- **THEN** the harness asserts that frame's decoded fields — the transport codes for the transport-routed frame, and for CONTROL the discovery subtype, tag and length form with a byte-identical rebuild — and the corpus documentation moves the shape out of its recorded-gap list while keeping the synthetic fixture

#### Scenario: Every corpus CONTROL frame decodes as discovery
- **WHEN** the corpus's CONTROL frames are decoded
- **THEN** each one decodes as a discovery request (6 or 10 bytes) or a discovery response (38 bytes), none falls back to uninterpreted, and the recorded counts of each form match the recorded expectation

#### Scenario: Decryption is corpus-verified for exactly one exchange
- **WHEN** the corpus documentation describes what the corpus proves about ciphertext
- **THEN** it states that sighop holds the key for the first-transmit session's direct messages and for no other encrypted payload in the corpus, so decryption is confirmed against live MeshCore traffic for that exchange while every other ciphertext remains verified only by round-trip and by fixed known-answer vectors
