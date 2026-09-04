## MODIFIED Requirements

> The corpus grows from 351 frames to 442: the two milestone 0 files
> (`captures/2026-09-02.jsonl` 152, `captures/2026-09-03.jsonl` 199) plus the milestone 2 live
> session (`captures/2026-09-04.jsonl` 56, `captures/2026-09-04-02.jsonl` 2,
> `captures/2026-09-04-03.jsonl` 33). The new files carry their provenance in-band as the
> `capture_meta` header this change introduced, rather than in a `.meta.json` sidecar. The
> session brought the first live `ROUTE_TYPE_TRANSPORT_FLOOD` frame and the first CONTROL
> payloads, which moves them out of the recorded-gap list.

### Requirement: Corpus replay
The system SHALL provide a test harness that reads every `rx_frame` record from each capture
file, decodes the frame through the packet and payload codecs, and fails the test run if any
frame fails to decode.

#### Scenario: All corpus frames decode
- **WHEN** the corpus replay test runs
- **THEN** all 442 `rx_frame` records decode without error and the test passes

#### Scenario: A regression breaks decoding of one frame
- **WHEN** a code change causes any single corpus frame to fail decoding
- **THEN** the test fails and names the capture file, the record index and the raw hex of the offending frame

#### Scenario: Corpus file is missing
- **WHEN** a capture file named by the harness is absent
- **THEN** the test fails with a clear error rather than silently passing on an empty corpus

#### Scenario: A capture file carrying a provenance header is replayed
- **WHEN** a corpus file whose first line is a `capture_meta` record is read by the harness
- **THEN** the header is consumed as provenance and never counted or decoded as a frame

### Requirement: Corpus distribution assertions
The system SHALL assert the aggregate composition of the decoded corpus — counts by payload
type, by route type, by hop count and by path hash size — against recorded expected values, so
that a decoder change which shifts how frames are classified fails loudly even when every
frame still decodes.

#### Scenario: Payload type distribution holds
- **WHEN** the corpus is decoded and payload types are counted
- **THEN** the counts match the recorded expectation: GRP_TXT 118, TXT_MSG 118, ADVERT 75, ACK 44, RESPONSE 23, ANON_REQ 22, PATH 15, REQ 12, CONTROL 6, GRP_DATA 5, TRACE 4

#### Scenario: Path hash size distribution holds
- **WHEN** the corpus is decoded and path hash sizes are counted
- **THEN** the counts match the recorded expectation of 197 frames with 1-byte hashes, 108 with 2-byte and 137 with 3-byte, which is the evidence that multi-byte path hashes are live on this mesh

#### Scenario: A misread header shifts the distribution
- **WHEN** a change causes payload types to be extracted from the wrong header bits
- **THEN** the distribution assertion fails even though individual frames may still parse

### Requirement: Advert verification over the corpus
The system SHALL verify the Ed25519 signature of every ADVERT frame in the corpus as part of
the test run, and SHALL fail if any does not verify.

#### Scenario: All corpus adverts verify
- **WHEN** the corpus replay verifies advert signatures
- **THEN** all 75 ADVERT frames pass verification

#### Scenario: Named nodes decode consistently
- **WHEN** the corpus adverts are parsed for appdata
- **THEN** the recovered node names and types match the recorded expectation, including multi-hop adverts whose names decode correctly only under the multi-byte path hash encoding, and the CHAT-type adverts the live session added alongside the repeater and room-server ones

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

#### Scenario: Decryption is not claimed to be corpus-verified
- **WHEN** the corpus documentation describes what the corpus proves
- **THEN** it states explicitly that sighop holds no key for any encrypted payload in it, so ciphertext handling is verified only by round-trip and by fixed known-answer vectors, and is not confirmed against live MeshCore traffic until the milestone 4 peer exchange

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
