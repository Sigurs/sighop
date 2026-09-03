## ADDED Requirements

> Reference: `related-repos/MeshCore/docs/packet_format.md` is the authoritative packet format
> document for every field below. `src/Packet.cpp` in the same submodule settles anything the
> document leaves ambiguous.

### Requirement: Packet header decoding
The system SHALL decode the 1-byte packet header as `0bVVPPPPRR`, extracting the route type
from bits 0-1 (mask `0x03`), the payload type from bits 2-5 (mask `0x3C`), and the payload
version from bits 6-7 (mask `0xC0`), and SHALL expose all three as named values rather than
raw integers.

#### Scenario: Flood-routed advert header
- **WHEN** a packet whose first byte is `0x11` is decoded
- **THEN** the result reports route type `FLOOD`, payload type `ADVERT`, and payload version 1

#### Scenario: Direct-routed group text header
- **WHEN** a packet whose first byte is `0x16` is decoded
- **THEN** the result reports route type `DIRECT`, payload type `GRP_TXT`, and payload version 1

#### Scenario: Unrecognized payload type value
- **WHEN** a packet header carries a payload type value that is reserved (`0x0C`-`0x0E`)
- **THEN** the decoder preserves the numeric value and marks the payload type as unrecognized, rather than raising or coercing it to a known type

#### Scenario: Unsupported payload version
- **WHEN** a packet header carries a payload version other than 1
- **THEN** the decoder rejects the packet as unsupported, reporting the observed version, rather than parsing its payload under v1 field sizes

### Requirement: Transport code presence
The system SHALL read the 4-byte transport code block (two little-endian `uint16` values)
immediately after the header if and only if the route type is `TRANSPORT_FLOOD` (`0x00`) or
`TRANSPORT_DIRECT` (`0x03`), and SHALL preserve both values on the decoded packet without
interpreting them.

#### Scenario: Transport-routed packet carries transport codes
- **WHEN** a packet with route type `TRANSPORT_FLOOD` is decoded
- **THEN** the four bytes following the header are decoded as two little-endian 16-bit transport codes, and the path length byte is read from the fifth byte after the header

#### Scenario: Non-transport packet has no transport codes
- **WHEN** a packet with route type `FLOOD` or `DIRECT` is decoded
- **THEN** no transport codes are read, the path length byte is read immediately after the header, and the decoded packet reports its transport codes as absent

### Requirement: Path length encoding
The system SHALL decode `path_length` as a packed byte in which bits 0-5 hold the hop count
(0-63) and bits 6-7 hold the path hash size minus one, and SHALL compute the path's byte
extent as `hop_count * hash_size` rather than treating the byte as a length.

#### Scenario: Zero-hop packet
- **WHEN** a packet's path length byte is `0x00`
- **THEN** the decoded packet has a hop count of 0, a hash size of 1, and an empty path, and the payload begins at the next byte

#### Scenario: Legacy 1-byte path hashes
- **WHEN** a packet's path length byte is `0x05`
- **THEN** the decoded path is the next 5 bytes, split into 5 single-byte hops

#### Scenario: 2-byte path hashes
- **WHEN** a packet's path length byte is `0x45`
- **THEN** the decoded path is the next 10 bytes, split into 5 two-byte hops

#### Scenario: 3-byte path hashes
- **WHEN** a packet's path length byte is `0x8A`
- **THEN** the decoded path is the next 30 bytes, split into 10 three-byte hops

#### Scenario: Reserved hash size code
- **WHEN** a packet's path length byte has bits 6-7 set to `0b11` (hash size 4)
- **THEN** the decoder rejects the packet as malformed, reporting the reserved hash size code, rather than reading 4-byte hops

### Requirement: Packet size validation
The system SHALL reject as malformed any packet whose path extent (`hop_count * hash_size`)
exceeds 64 bytes (`MAX_PATH_SIZE`), whose payload exceeds 184 bytes (`MAX_PACKET_PAYLOAD`),
whose total length exceeds 255 bytes, or which is truncated before the end of a field the
header declares, and SHALL identify which limit was violated.

#### Scenario: Path extent exceeds MAX_PATH_SIZE
- **WHEN** a packet declares 23 hops with 3-byte hashes (69 path bytes)
- **THEN** the decoder rejects it as malformed, naming the path-size limit, even though the hop count alone is within range

#### Scenario: Payload exceeds MAX_PACKET_PAYLOAD
- **WHEN** a packet's remaining bytes after the path exceed 184 bytes
- **THEN** the decoder rejects it as malformed, naming the payload-size limit

#### Scenario: Packet truncated mid-path
- **WHEN** a packet declares a hop count and hash size requiring more path bytes than remain in the buffer
- **THEN** the decoder rejects it as malformed, naming truncation, rather than reading past the end or silently shortening the path

#### Scenario: Empty payload
- **WHEN** a packet's bytes end exactly at the end of its declared path
- **THEN** the decoder produces a packet with an empty payload rather than rejecting it, leaving payload-level validation to the payload codec

### Requirement: Packet encoding
The system SHALL encode a packet structure back into wire bytes, emitting the header, the
transport codes when and only when the route type requires them, the packed path length byte,
the path bytes, and the payload, and SHALL apply the same size limits it enforces on decode.

#### Scenario: Encode round-trips a decoded packet
- **WHEN** any packet from the capture corpus is decoded and then re-encoded
- **THEN** the resulting bytes are byte-for-byte identical to the original frame

#### Scenario: Encoding computes the path length byte
- **WHEN** a packet is encoded with 5 hops of 2-byte hashes
- **THEN** the emitted path length byte is `0x45` and 10 path bytes follow it

#### Scenario: Encoding rejects an over-limit payload
- **WHEN** a packet is encoded with a payload longer than 184 bytes
- **THEN** encoding fails with an error naming the payload-size limit rather than emitting an oversized packet

### Requirement: Decoded packets are inert data
The system SHALL expose decoded packets as immutable structures over `bytes`, with no
dependency on the radio, database, network or scheduler layers, so that the packet codec is
testable in isolation from captured bytes alone.

#### Scenario: Codec imports nothing above it
- **WHEN** the packet codec module is imported in a process with no serial device, no database and no event loop
- **THEN** it imports and decodes captured frames successfully
