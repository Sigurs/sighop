## MODIFIED Requirements

> **The counts below were measured, not planned.** The first-transmit session was recorded on
> 2026-09-04 as `captures/2026-09-04-first-transmit.jsonl`: 6 frames, of which 3 are frames
> sighop transmitted. The corpus therefore holds **1003 frame records — 1000 received and 3
> transmitted**, against milestone 3's 997. Every frame of the new session is `DIRECT` with an
> empty path, so the deltas are `TXT_MSG` +2, `ACK` +2, `ADVERT` +2, `DIRECT` +6, hop count 0 +6,
> path hash size 1 +6, and nothing else moved.

### Requirement: Corpus replay
The system SHALL provide a test harness that reads every frame record from each capture file,
decodes the frame through the packet and payload codecs, and fails the test run if any frame
fails to decode. Frames sighop transmitted SHALL be replayed through the same decoders as
frames it received.

#### Scenario: All corpus frames decode
- **WHEN** the corpus replay test runs
- **THEN** all 1003 frame records decode without error and the test passes

#### Scenario: A regression breaks decoding of one frame
- **WHEN** a code change causes any single corpus frame to fail decoding
- **THEN** the test fails and names the capture file, the record index and the raw hex of the offending frame

#### Scenario: Corpus file is missing
- **WHEN** a capture file named by the harness is absent
- **THEN** the test fails with a clear error rather than silently passing on an empty corpus

#### Scenario: A capture file carrying a provenance header is replayed
- **WHEN** a corpus file whose first line is a `capture_meta` record is read by the harness
- **THEN** the header is consumed as provenance and never counted or decoded as a frame

#### Scenario: A transmitted frame is replayed
- **WHEN** a record the runtime wrote for a frame it transmitted is read by the harness
- **THEN** it decodes through the same packet and payload codecs as a received frame, and is counted separately from receptions

### Requirement: Corpus distribution assertions
The system SHALL assert the aggregate composition of the decoded corpus — counts by payload
type, by route type, by hop count and by path hash size — against recorded expected values, so
that a decoder change which shifts how frames are classified fails loudly even when every
frame still decodes.

#### Scenario: Payload type distribution holds
- **WHEN** the corpus is decoded and payload types are counted
- **THEN** the counts match the recorded expectation, updated by this change to include the first-transmit session's frames

#### Scenario: Path hash size distribution holds
- **WHEN** the corpus is decoded and path hash sizes are counted
- **THEN** the counts match the recorded expectation, which continues to show multi-byte path hashes live on this mesh

#### Scenario: A misread header shifts the distribution
- **WHEN** a change causes payload types to be extracted from the wrong header bits
- **THEN** the distribution assertion fails even though individual frames may still parse

### Requirement: Corpus coverage is recorded, including its gaps
The system SHALL record, alongside the corpus, which packet and payload shapes it does and
does not exercise, so that synthetic tests are written deliberately for the gaps rather than
coverage being assumed, and SHALL assert a shape the corpus has since acquired against the
recorded frames rather than leaving it to a synthetic fixture alone.

#### Scenario: Gaps are covered by synthetic fixtures
- **WHEN** a shape absent from the corpus is identified — `ROUTE_TYPE_TRANSPORT_DIRECT` packets, MULTIPART, RAW_CUSTOM, reserved payload types, hop counts above 5, and the reserved 4-byte hash size code
- **THEN** a synthetic fixture exists for it in the test suite, and the corpus documentation states that the corpus itself does not cover it

#### Scenario: The corpus acquires a shape that was previously synthetic-only
- **WHEN** a live capture appended to the corpus contains a `ROUTE_TYPE_TRANSPORT_FLOOD` frame or a CONTROL payload
- **THEN** the harness asserts that frame's decoded fields — the transport codes for the transport-routed frame, preservation of the uninterpreted bytes for CONTROL — and the corpus documentation moves the shape out of its recorded-gap list while keeping the synthetic fixture

#### Scenario: Decryption is corpus-verified for exactly one exchange
- **WHEN** the corpus documentation describes what the corpus proves about ciphertext
- **THEN** it states that sighop holds the key for the first-transmit session's direct messages and for no other encrypted payload in the corpus, so decryption is confirmed against live MeshCore traffic for that exchange while every other ciphertext remains verified only by round-trip and by fixed known-answer vectors

## ADDED Requirements

### Requirement: The corpus admits frames sighop transmitted
The system SHALL allow a capture to contain records of frames sighop transmitted alongside the
frames it received, SHALL distinguish the two by record kind, and SHALL keep transmitted frames
out of any assertion whose subject is what the mesh sent us.

#### Scenario: A session containing our own transmissions is appended
- **WHEN** a capture from a run with transmission enabled is appended to the corpus
- **THEN** its transmitted frames are recorded with a distinct record kind, decode through the same codecs, and are excluded from reception-derived measurements such as duplicate rate

### Requirement: The first-transmit session is appended whole and its decrypt vector extracted
The system SHALL append the first-transmit session to the corpus in its entirety, per the rule
that a session is appended whole rather than as hand-picked frames, and SHALL identify within it
the specific records that constitute the first decryptable direct message, the acknowledgement
it produced, and the message sighop sent that the peer acknowledged.

#### Scenario: Session appended
- **WHEN** the first-transmit session is added to the corpus
- **THEN** every frame of the session is included, its `capture_meta` header records the provenance of both boards involved, and the counts and coverage notes are updated to the measured figures

#### Scenario: The decrypt vector is locatable
- **WHEN** the known-answer test for foreign-implementation decryption is read
- **THEN** it names the capture file and record index of the ciphertext it decrypts, so the vector and its provenance cannot drift apart
