# protocol-corpus Specification

## Purpose
The captured-frame regression corpus: how recorded live traffic is replayed through the packet
and payload codecs, what aggregate properties are asserted over it, and how its provenance and
its known gaps are kept on the record as the corpus grows.
## Requirements

> The corpus stands at 997 frames across six files: the two milestone 0 files
> (`captures/2026-09-02.jsonl` 152, `captures/2026-09-03.jsonl` 199), the milestone 2 live
> session (`captures/2026-09-04.jsonl` 56, `captures/2026-09-04-02.jsonl` 2,
> `captures/2026-09-04-03.jsonl` 33) and the milestone 3 long receive-only run
> (`captures/2026-09-05.jsonl` 555, 2 h 54 min on the Heltec V4). Every file from milestone 2
> onward carries its provenance in-band as a `capture_meta` header rather than a `.meta.json`
> sidecar. Milestone 2 brought the first live `ROUTE_TYPE_TRANSPORT_FLOOD` frame and the first
> CONTROL payloads, moving them out of the recorded-gap list; milestone 3 brought a located
> CHAT advert (flags `0x91`), a 10-byte TRACE, the duplicate-timing tail that sizes the dedup
> TTL, and enough CONTROL traffic (168 frames) to show it is ordinary rather than a curiosity.

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
- **THEN** it contains structural fields and ciphertext digests only — never decrypted plaintext, since the corpus frames are other people's traffic and sighop holds no key for them

### Requirement: Round-trip re-encoding of the corpus
The system SHALL re-encode every decoded corpus frame and assert the result is byte-identical
to the original frame, proving the codec loses nothing it decoded.

#### Scenario: Every frame round-trips
- **WHEN** each corpus frame is decoded and re-encoded
- **THEN** the re-encoded bytes equal the original raw bytes exactly, including transport codes, path bytes and payload

### Requirement: Advert verification over the corpus
The system SHALL verify the Ed25519 signature of every ADVERT frame in the corpus as part of
the test run, and SHALL fail if any does not verify.

#### Scenario: All corpus adverts verify
- **WHEN** the corpus replay verifies advert signatures
- **THEN** all 92 ADVERT frames pass verification

#### Scenario: Named nodes decode consistently
- **WHEN** the corpus adverts are parsed for appdata
- **THEN** the recovered node names and types match the recorded expectation, including multi-hop adverts whose names decode correctly only under the multi-byte path hash encoding, the CHAT-type adverts the milestone 2 session added alongside the repeater and room-server ones, and the located CHAT advert (`0x91`) the milestone 3 session added — the first live frame setting the location bit on a node that is not a repeater or room server

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

### Requirement: Corpus provenance is preserved
The system SHALL leave the capture files and their provenance unmodified by the test harness,
treating them as read-only evidence, and SHALL require every corpus file to carry its
recording conditions either as a `capture_meta` header record or as a paired `.meta.json`
sidecar.

#### Scenario: Test run does not mutate the corpus
- **WHEN** the corpus replay test runs
- **THEN** neither the `.jsonl` files nor their sidecars are written to, and the test opens them read-only

#### Scenario: A corpus file states no recording conditions
- **WHEN** a capture file in the corpus has neither a `capture_meta` first line nor a `.meta.json` sidecar
- **THEN** the test run fails, because a capture of unrecorded origin is a fixture rather than evidence

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

