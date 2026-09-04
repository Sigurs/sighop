## MODIFIED Requirements

> Reference: DESIGN.md §12 (the corpus is not frozen; a session is appended whole when it
> carries a shape the corpus lacks). Milestone 3's long receive-only run added
> `captures/2026-09-05.jsonl` — 555 frames over 2 h 54 min on the Heltec V4, provenance in-band
> as a `capture_meta` header — taking the corpus from 442 frames across five files to **997
> across six**. It brought three shapes the corpus lacked: a **located CHAT advert** (flags
> `0x91`, where every chat advert before it was `0x81` with no location), a **10-byte TRACE**
> against 13 and 21 bytes previously, and the duplicate-timing tail that sizes the dedup TTL —
> a genuine flood copy 200.7 s late by a different path, and a byte-identical sender
> retransmission 3158 s later that is deliberately *not* treated as a duplicate. It also showed
> CONTROL to be ordinary traffic rather than a curiosity: 162 frames against six in the entire
> corpus before it. Only the recorded counts change here; every rule the requirements state is
> unchanged.

### Requirement: Corpus replay
The system SHALL provide a test harness that reads every `rx_frame` record from each capture
file, decodes the frame through the packet and payload codecs, and fails the test run if any
frame fails to decode.

#### Scenario: All corpus frames decode
- **WHEN** the corpus replay test runs
- **THEN** all 997 `rx_frame` records decode without error and the test passes

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
- **THEN** the counts match the recorded expectation: GRP_TXT 341, TXT_MSG 191, CONTROL 168, ADVERT 92, ACK 67, ANON_REQ 42, PATH 38, RESPONSE 36, REQ 12, GRP_DATA 5, TRACE 5

#### Scenario: Path hash size distribution holds
- **WHEN** the corpus is decoded and path hash sizes are counted
- **THEN** the counts match the recorded expectation of 487 frames with 1-byte hashes, 109 with 2-byte and 401 with 3-byte, which is the evidence that multi-byte path hashes are live on this mesh

#### Scenario: A misread header shifts the distribution
- **WHEN** a change causes payload types to be extracted from the wrong header bits
- **THEN** the distribution assertion fails even though individual frames may still parse

### Requirement: Advert verification over the corpus
The system SHALL verify the Ed25519 signature of every ADVERT frame in the corpus as part of
the test run, and SHALL fail if any does not verify.

#### Scenario: All corpus adverts verify
- **WHEN** the corpus replay verifies advert signatures
- **THEN** all 92 ADVERT frames pass verification

#### Scenario: Named nodes decode consistently
- **WHEN** the corpus adverts are parsed for appdata
- **THEN** the recovered node names and types match the recorded expectation, including multi-hop adverts whose names decode correctly only under the multi-byte path hash encoding, the CHAT-type adverts the milestone 2 session added alongside the repeater and room-server ones, and the located CHAT advert (`0x91`) the milestone 3 session added — the first live frame setting the location bit on a node that is not a repeater or room server
