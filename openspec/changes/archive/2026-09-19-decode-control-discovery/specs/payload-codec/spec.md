# Spec Delta

## ADDED Requirements

### Requirement: Node discovery control payload parsing
The system SHALL parse a CONTROL payload whose first byte's upper nibble is `0x80`
(`NODE_DISCOVER_REQ`) or `0x90` (`NODE_DISCOVER_RESP`) into a structured discovery payload,
following `examples/simple_repeater/MyMesh.cpp::onControlDataRecv`. All multi-byte fields are
little-endian.

A discovery request SHALL be exactly 6 or 10 bytes: the control byte, whose bit 0 is the
prefix-only flag and whose bits 1–3 are preserved as given; a one-byte node-type filter, a bit
field of `1 << node type`; a 4-byte tag; and, in the 10-byte form only, a 4-byte `since`
timestamp. A 6-byte request SHALL report `since` as absent, not as zero.

A discovery response SHALL be exactly 14 or 38 bytes: the control byte, whose low nibble is the
responder's node type; one signed byte giving the SNR at which the responder heard the request,
in quarter-dB units; the 4-byte tag copied from the request; and the responder's public key,
either an 8-byte prefix (14-byte form) or the full 32 bytes (38-byte form). The key SHALL be
exposed as a claim the payload does not authenticate. A discovery response carries no signature.

A payload whose subtype is a discovery subtype but whose length is not one of that subtype's
valid lengths SHALL fail with a bad-payload-length failure that names the valid lengths.

#### Scenario: A discovery request without a since field
- **WHEN** the CONTROL payload `80 04 9a 7d 39 16` is parsed
- **THEN** the result is a discovery request with prefix-only false, a filter selecting REPEATER, tag `9a7d3916`, and `since` absent

#### Scenario: A discovery request with a since field
- **WHEN** a 10-byte CONTROL payload beginning `0x80` is parsed
- **THEN** the result is a discovery request carrying the filter, the tag and the 4-byte `since` value

#### Scenario: A discovery response carrying a full key
- **WHEN** a 38-byte CONTROL payload beginning `0x92` is parsed
- **THEN** the result is a discovery response with node type REPEATER, the reported SNR in dB (the signed byte divided by 4), the tag, and a 32-byte claimed public key

#### Scenario: A discovery response carrying a key prefix
- **WHEN** a 14-byte CONTROL payload beginning `0x92` is parsed
- **THEN** the result is a discovery response whose claimed key is the 8-byte prefix, marked as a prefix and not a full key

#### Scenario: A discovery payload of a length the firmware never sends
- **WHEN** a CONTROL payload beginning `0x80` is 7 bytes long, or one beginning `0x90` is 20 bytes long
- **THEN** parsing fails with a bad-payload-length failure naming the valid lengths for that subtype, and the header and path stay observable

#### Scenario: A node type value outside the known enumeration
- **WHEN** a discovery response's low nibble is a value with no named node type
- **THEN** the response still parses and reports the raw node type value

## MODIFIED Requirements

### Requirement: Unsupported payload types are preserved, not dropped
The system SHALL represent MULTIPART, RAW_CUSTOM, reserved payload types, and every CONTROL
payload that is not a node discovery request or response (including an empty CONTROL payload),
as a recognized-but-unparsed payload carrying the raw bytes, rather than raising or discarding
them.

#### Scenario: Multipart payload encountered
- **WHEN** a packet with payload type `0x0A` (MULTIPART) is decoded
- **THEN** the result reports the payload type and preserves the raw payload bytes without attempting to reassemble the sequence, which is an explicit v1 non-goal

#### Scenario: A CONTROL subtype that is not discovery
- **WHEN** a CONTROL payload whose first byte's upper nibble is neither `0x80` nor `0x90` is decoded, for example `de ad`
- **THEN** the result is the recognized-but-unparsed payload carrying those bytes, not a failure

### Requirement: Payload building
The system SHALL build the wire bytes for ADVERT, ACK, the REQ/RESPONSE/TXT_MSG/PATH
envelope, ANON_REQ, GRP_TXT, GRP_DATA and node discovery request and response payloads, and
for the plaintext bodies those envelopes carry, applying the same field layouts and limits it
enforces on parse. Building a returned path body SHALL accept a bundled extra payload of a
stated type and emit it verbatim after the path hashes, so that a reply carrying both a route
and an answer inside it can be composed.

#### Scenario: Build round-trips a parsed payload
- **WHEN** any parseable payload from the capture corpus is parsed and then rebuilt
- **THEN** the resulting bytes are byte-for-byte identical to the original payload

#### Scenario: Advert appdata exceeds its limit
- **WHEN** an advert is built with appdata longer than 32 bytes (`MAX_ADVERT_DATA_SIZE`)
- **THEN** building fails with an error naming the limit rather than emitting an advert the firmware would reject

#### Scenario: A returned path bundling a response
- **WHEN** a returned path body is built with a path and a bundled payload of a stated type
- **THEN** the bytes carry the packed hop-count and hash-size byte, the path hashes, the extra type byte and the bundled payload unchanged, and parsing them yields what was supplied

#### Scenario: A discovery payload with an invalid field size
- **WHEN** a discovery response is built with a claimed key that is neither 8 nor 32 bytes, or a discovery request with a tag that is not 4 bytes
- **THEN** building fails with an error naming the field, rather than emitting bytes the parser would reject
