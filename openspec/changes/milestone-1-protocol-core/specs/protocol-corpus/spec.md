## ADDED Requirements

> The corpus is `captures/2026-09-02.jsonl` (152 frames) and `captures/2026-09-03.jsonl`
> (199 frames), with provenance in the paired `.meta.json` sidecars. Per DESIGN.md §12 these
> files are the permanent regression corpus for the protocol layer.

### Requirement: Corpus replay
The system SHALL provide a test harness that reads every `rx_frame` record from each capture
file, decodes the frame through the packet and payload codecs, and fails the test run if any
frame fails to decode.

#### Scenario: All corpus frames decode
- **WHEN** the corpus replay test runs
- **THEN** all 351 `rx_frame` records decode without error and the test passes

#### Scenario: A regression breaks decoding of one frame
- **WHEN** a code change causes any single corpus frame to fail decoding
- **THEN** the test fails and names the capture file, the record index and the raw hex of the offending frame

#### Scenario: Corpus file is missing
- **WHEN** a capture file named by the harness is absent
- **THEN** the test fails with a clear error rather than silently passing on an empty corpus

### Requirement: Corpus distribution assertions
The system SHALL assert the aggregate composition of the decoded corpus — counts by payload
type, by route type, by hop count and by path hash size — against recorded expected values, so
that a decoder change which shifts how frames are classified fails loudly even when every
frame still decodes.

#### Scenario: Payload type distribution holds
- **WHEN** the corpus is decoded and payload types are counted
- **THEN** the counts match the recorded expectation: TXT_MSG 108, GRP_TXT 91, ADVERT 53, ACK 42, ANON_REQ 16, PATH 15, RESPONSE 11, REQ 6, GRP_DATA 5, TRACE 4

#### Scenario: Path hash size distribution holds
- **WHEN** the corpus is decoded and path hash sizes are counted
- **THEN** the counts match the recorded expectation of 152 frames with 1-byte hashes, 106 with 2-byte and 93 with 3-byte, which is the evidence that multi-byte path hashes are live on this mesh

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
- **THEN** all 53 ADVERT frames pass verification

#### Scenario: Named nodes decode consistently
- **WHEN** the corpus adverts are parsed for appdata
- **THEN** the recovered node names and types match the recorded expectation, including multi-hop adverts whose names decode correctly only under the multi-byte path hash encoding

### Requirement: Corpus coverage is recorded, including its gaps
The system SHALL record, alongside the corpus, which packet and payload shapes it does and
does not exercise, so that synthetic tests are written deliberately for the gaps rather than
coverage being assumed.

#### Scenario: Gaps are covered by synthetic fixtures
- **WHEN** a shape absent from the corpus is identified — `ROUTE_TYPE_TRANSPORT_FLOOD` and `ROUTE_TYPE_TRANSPORT_DIRECT` packets, MULTIPART, CONTROL, RAW_CUSTOM, reserved payload types, hop counts above 5, and the reserved 4-byte hash size code
- **THEN** a synthetic fixture exists for it in the test suite, and the corpus documentation states that the corpus itself does not cover it

#### Scenario: Decryption is not claimed to be corpus-verified
- **WHEN** the corpus documentation describes what the corpus proves
- **THEN** it states explicitly that every encrypted payload in it is addressed to a third party, so ciphertext handling is verified only by round-trip and by fixed known-answer vectors, and is not confirmed against live MeshCore traffic until the milestone 4 peer exchange

### Requirement: Corpus provenance is preserved
The system SHALL leave the capture files and their `.meta.json` provenance sidecars unmodified
by the test harness, treating them as read-only evidence.

#### Scenario: Test run does not mutate the corpus
- **WHEN** the corpus replay test runs
- **THEN** neither the `.jsonl` files nor their sidecars are written to, and the test opens them read-only
