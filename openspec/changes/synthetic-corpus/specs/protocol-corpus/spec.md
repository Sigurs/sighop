# Spec Delta

## MODIFIED Requirements

### Requirement: Corpus replay
The system SHALL provide a test harness that reads every frame record from each corpus file,
decodes the frame through the packet and payload codecs, and fails the test run if any frame
fails to decode. Frames sighop transmitted SHALL be replayed through the same decoders as
frames it received.

#### Scenario: All corpus frames decode
- **WHEN** the corpus replay test runs
- **THEN** every frame record decodes without error, the number decoded equals the recorded expected count, and the test passes

#### Scenario: A regression breaks decoding of one frame
- **WHEN** a code change causes any single corpus frame to fail decoding
- **THEN** the test fails and names the corpus file, the record index and the raw hex of the offending frame

#### Scenario: Corpus file is missing
- **WHEN** a corpus file named by the harness is absent
- **THEN** the test fails with a clear error rather than silently passing on an empty corpus

#### Scenario: A capture file carrying a provenance header is replayed
- **WHEN** a corpus file whose first line is a `capture_meta` record is read by the harness
- **THEN** the header is consumed as provenance and never counted or decoded as a frame

#### Scenario: A transmitted frame is replayed
- **WHEN** a record the corpus holds for a frame sighop transmitted is read by the harness
- **THEN** it decodes through the same packet and payload codecs as a received frame, and is counted separately from receptions

### Requirement: Corpus distribution assertions
The system SHALL assert the aggregate composition of the decoded corpus — counts by payload
type, by route type, by hop count and by path hash size — against recorded expected values, so
that a decoder change which shifts how frames are classified fails loudly even when every
frame still decodes.

#### Scenario: Payload type distribution holds
- **WHEN** the corpus is decoded and payload types are counted
- **THEN** the counts match the recorded expectation

#### Scenario: Path hash size distribution holds
- **WHEN** the corpus is decoded and path hash sizes are counted
- **THEN** the counts match the recorded expectation, which shows all three live path hash sizes

#### Scenario: A misread header shifts the distribution
- **WHEN** a change causes payload types to be extracted from the wrong header bits
- **THEN** the distribution assertion fails even though individual frames may still parse

### Requirement: Golden snapshot of decoded output
The system SHALL maintain a checked-in golden file holding the decoded, human-readable form of
every corpus frame, and SHALL fail the test run when current decoding differs from it, so that
changes in interpretation are visible as a reviewable diff rather than an invisible drift.

#### Scenario: Decoded output matches the golden file
- **WHEN** the corpus is decoded and rendered to the golden format
- **THEN** the output is byte-identical to the checked-in golden file

#### Scenario: An intentional decoding improvement
- **WHEN** a decoder change alters the rendered output and the golden file is regenerated
- **THEN** the diff shows exactly which frames changed and how, and is reviewable as part of the change

#### Scenario: Golden file contains no decrypted content
- **WHEN** the golden file is generated
- **THEN** it contains structural fields and ciphertext digests only — never decrypted plaintext, even though the synthetic corpus's keys are known, so the rule holds if a recorded frame is ever added

### Requirement: Advert verification over the corpus
The system SHALL verify the Ed25519 signature of every ADVERT frame in the corpus as part of
the test run, and SHALL fail if any does not verify.

#### Scenario: All corpus adverts verify
- **WHEN** the corpus replay verifies advert signatures
- **THEN** every ADVERT frame passes verification and their number equals the recorded expectation

#### Scenario: Named nodes decode consistently
- **WHEN** the corpus adverts are parsed for appdata
- **THEN** the recovered node names and types match the recorded expectation, including multi-hop adverts whose names decode correctly only under the multi-byte path hash encoding, chat-type adverts with and without a location, and room-server adverts with and without a location

### Requirement: Corpus provenance is preserved
The system SHALL leave the corpus files unmodified by the test harness, treating them as read-only
generated artefacts that only the generator writes, and SHALL require every corpus file to carry its
provenance as a `capture_meta` header record identifying it as synthetic with its generator version
and seed.

#### Scenario: Test run does not mutate the corpus
- **WHEN** the corpus replay test runs
- **THEN** the `.jsonl` files are not written to, and the test opens them read-only

#### Scenario: A corpus file states no recording conditions
- **WHEN** a file in the corpus has no `capture_meta` first line
- **THEN** the test run fails, because a file of unrecorded origin is neither generated nor evidence

## ADDED Requirements

### Requirement: Corpus coverage is recorded, and its limits are stated
The system SHALL record, alongside the corpus, which packet and payload shapes it does and
does not exercise, so that synthetic tests are written deliberately for the gaps rather than
coverage being assumed, and SHALL assert a shape the corpus holds against its frames rather
than leaving it to a hand-built fixture alone.

#### Scenario: Gaps are covered by hand-built fixtures
- **WHEN** a shape absent from the corpus is identified — `ROUTE_TYPE_TRANSPORT_DIRECT` packets, MULTIPART, RAW_CUSTOM, reserved payload types, CONTROL subtypes other than node discovery, the 14-byte key-prefix discovery response, hop counts above 5, and the reserved 4-byte hash size code
- **THEN** a fixture exists for it in the test suite, and the corpus documentation states that the corpus itself does not cover it

#### Scenario: A shape the corpus holds is asserted against its frames
- **WHEN** the corpus contains a `ROUTE_TYPE_TRANSPORT_FLOOD` frame or a CONTROL payload
- **THEN** the harness asserts that frame's decoded fields — the transport codes for the transport-routed frame, and for CONTROL the discovery subtype, tag and length form with a byte-identical rebuild — while keeping the hand-built fixture

#### Scenario: Every corpus CONTROL frame decodes as discovery
- **WHEN** the corpus's CONTROL frames are decoded
- **THEN** each one decodes as a discovery request (6 or 10 bytes) or a discovery response (38 bytes), none falls back to uninterpreted, and the recorded counts of each form match the recorded expectation

#### Scenario: The corpus states what it cannot prove
- **WHEN** the corpus documentation describes what a green corpus run establishes
- **THEN** it states that the corpus is generated by sighop's own encoders, so it proves the decoders agree with the encoders and the recorded expectations, and does not prove interoperability with any other MeshCore implementation, which the earlier recorded corpus once did for one exchange and one channel

## REMOVED Requirements

### Requirement: Corpus coverage is recorded, including its gaps
**Reason**: The requirement's scenarios describe a recorded corpus that acquired shapes over live sessions, and end by requiring the documentation to say decryption is verified for exactly one recorded exchange. Neither holds for a generated corpus, and the requirement cannot be edited in place without keeping a scenario whose name asserts the withdrawn claim.
**Migration**: Replaced by "Corpus coverage is recorded, and its limits are stated", which keeps the gap list, the asserted-shape scenarios and the CONTROL scenario, and states in place of the one-exchange claim that the corpus proves no interoperability.


### Requirement: The first-transmit session is appended whole and its decrypt vector extracted
**Reason**: The session was a live exchange between the operator's two dev boards, decryptable with a
committed private key. Both keys, the boards' names and the recording itself are real data, and the
key was published in the repository. The requirement's purpose — proving decryption against a
foreign implementation — cannot be met by a generated corpus.
**Migration**: The synthetic exchange file holds a generated direct-message exchange with both
acknowledgement forms, whose decryption is asserted against the generator's own keys. The
foreign-implementation claim is withdrawn in `mesh-crypto`; the recording remains in git history only.
