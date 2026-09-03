## ADDED Requirements

> Reference: `related-repos/MeshCore/docs/payloads.md` is the authoritative payload document.
> `src/helpers/AdvertDataHelpers.cpp`, `src/Mesh.cpp` and `src/MeshCore.h` in the same
> submodule settle field order, sizes and constants where the document is ambiguous.
> All multi-byte integers are little-endian.

### Requirement: Advert payload parsing
The system SHALL parse an ADVERT payload as a 32-byte Ed25519 public key, a 4-byte
little-endian unix timestamp, a 64-byte Ed25519 signature, and a remaining appdata block,
and SHALL reject as malformed any ADVERT payload shorter than 101 bytes (the fixed 100 bytes
plus at least the appdata flags byte).

#### Scenario: Advert from the capture corpus
- **WHEN** any ADVERT packet in the capture corpus is parsed
- **THEN** the parse yields a 32-byte public key, a plausible unix timestamp, a 64-byte signature and a non-empty appdata block

#### Scenario: Advert truncated before its signature
- **WHEN** an ADVERT payload is 80 bytes long
- **THEN** the parse fails as malformed rather than reading past the end of the payload

### Requirement: Advert appdata parsing
The system SHALL parse the appdata block as a flags byte in which the **low nibble is a node
type enum** (`0` none, `1` chat, `2` repeater, `3` room server, `4` sensor) and the **high
nibble is a bit field** (`0x10` has location, `0x20` has feature 1, `0x40` has feature 2,
`0x80` has name), followed in that order by the optional fields the high nibble declares:
two 4-byte little-endian signed integers for latitude and longitude (each the decimal degree
value multiplied by 1,000,000), a 2-byte feature 1, a 2-byte feature 2, and finally the name
as the remainder of the appdata.

#### Scenario: Located, named repeater advert
- **WHEN** an advert's appdata flags byte is `0x92`
- **THEN** the parse reports node type `REPEATER`, decodes 8 bytes of latitude/longitude following the flags byte, and takes the name from the remaining bytes

#### Scenario: Located, named room server advert
- **WHEN** an advert's appdata flags byte is `0x93`
- **THEN** the parse reports node type `ROOM_SERVER` — not a bitwise combination of chat and repeater — and decodes location and name as above

#### Scenario: Advert with no name flag
- **WHEN** an advert's appdata flags byte does not set `0x80`
- **THEN** the parse reports no name, and any bytes remaining after the declared optional fields are preserved as trailing appdata rather than being decoded as a name

#### Scenario: Name is not NUL-terminated on the wire
- **WHEN** an advert's name occupies the remainder of the appdata with no terminator
- **THEN** the parse takes the whole remainder as the name

#### Scenario: Name is not valid UTF-8
- **WHEN** an advert's name bytes do not decode as UTF-8
- **THEN** the parse succeeds, exposing the raw name bytes and a replacement-character rendering flagged as invalid, rather than raising or discarding the advert

#### Scenario: Appdata truncated before a declared optional field
- **WHEN** an advert's flags declare a location but fewer than 8 bytes follow the flags byte
- **THEN** the parse fails as malformed

### Requirement: Encrypted envelope parsing
The system SHALL parse REQ, RESPONSE, TXT_MSG and PATH payloads as a shared envelope — 1-byte
destination hash, 1-byte source hash, 2-byte cipher MAC, then ciphertext as the remainder —
and SHALL reject as malformed any such payload shorter than 4 bytes or whose ciphertext length
is not a positive multiple of 16.

#### Scenario: Envelope payloads in the capture corpus
- **WHEN** any REQ, RESPONSE, TXT_MSG or PATH packet in the capture corpus is parsed
- **THEN** the parse yields destination and source hashes, a 2-byte MAC, and a ciphertext whose length is a positive multiple of the 16-byte cipher block

#### Scenario: Ciphertext not a whole number of cipher blocks
- **WHEN** an envelope payload's ciphertext is 20 bytes long
- **THEN** the parse fails as malformed, since AES-128-ECB output is always a multiple of 16

### Requirement: Anonymous request parsing
The system SHALL parse an ANON_REQ payload as a 1-byte destination hash, the sender's 32-byte
Ed25519 public key, a 2-byte cipher MAC, and ciphertext as the remainder — noting that it
carries a full public key where the other envelopes carry a 1-byte source hash.

#### Scenario: ANON_REQ from the capture corpus
- **WHEN** any ANON_REQ packet in the capture corpus is parsed
- **THEN** the parse yields a destination hash, a 32-byte sender public key whose first byte is the sender's node hash, a 2-byte MAC, and a block-aligned ciphertext

### Requirement: Group payload parsing
The system SHALL parse GRP_TXT and GRP_DATA payloads as a 1-byte channel hash, a 2-byte
cipher MAC, and ciphertext as the remainder.

#### Scenario: Group text from the capture corpus
- **WHEN** any GRP_TXT packet in the capture corpus is parsed
- **THEN** the parse yields a channel hash, a 2-byte MAC and a block-aligned ciphertext

### Requirement: Acknowledgement parsing
The system SHALL parse an ACK payload as a single 4-byte little-endian CRC32 checksum and
SHALL reject an ACK payload of any other length.

#### Scenario: ACK from the capture corpus
- **WHEN** any ACK packet in the capture corpus is parsed
- **THEN** the parse yields exactly one 4-byte checksum value and consumes the whole payload

### Requirement: Plain text message body parsing
The system SHALL parse a decrypted text-message body as a 4-byte little-endian timestamp, a
byte whose upper six bits are the `txt_type` and lower two bits are the attempt number (0-3),
and the message text as the remainder, and SHALL strip the zero padding that AES-128-ECB
block alignment leaves on the end of the text.

#### Scenario: Plain text message
- **WHEN** a decrypted body has `txt_type` 0
- **THEN** the parse reports a plain text message with its timestamp, attempt number and text

#### Scenario: CLI command message
- **WHEN** a decrypted body has `txt_type` 1
- **THEN** the parse reports a CLI command rather than a plain text message

#### Scenario: Signed plain text message
- **WHEN** a decrypted body has `txt_type` 2
- **THEN** the parse reports the first four bytes of the message as the sender public key prefix and the remainder as the text

#### Scenario: Zero padding is stripped from the text
- **WHEN** a decrypted body's text is followed by zero bytes added to reach a 16-byte boundary
- **THEN** the parsed text excludes those trailing zero bytes

### Requirement: Group text sender name is unverified
The system SHALL parse a decrypted GRP_TXT body in the plain-text-message layout, split the
text on the first `": "` into a sender name and a body, and SHALL mark the resulting sender
name as unauthenticated in the parsed structure so that no consumer can present it as a
verified identity.

#### Scenario: Group message with a sender name prefix
- **WHEN** a decrypted group message text is `user123: I'm on my way`
- **THEN** the parse reports sender name `user123` and body `I'm on my way`, with the sender name explicitly flagged as unverified

#### Scenario: Group message with no name separator
- **WHEN** a decrypted group message text contains no `": "` separator
- **THEN** the parse reports no sender name and the whole text as the body, rather than guessing a split

### Requirement: Returned path body parsing
The system SHALL parse a decrypted PATH body as a 1-byte path length, that many single-byte
path hashes, a 1-byte extra payload type using the same values as the packet header's payload
type field, and the remaining bytes as the bundled extra payload, parsed according to that
type.

#### Scenario: Returned path bundling an acknowledgement
- **WHEN** a decrypted PATH body declares extra type `0x03` (ACK)
- **THEN** the parse yields the path hashes and an ACK checksum parsed from the extra bytes

#### Scenario: Returned path with no bundled extra
- **WHEN** a decrypted PATH body ends immediately after its path hashes
- **THEN** the parse yields the path hashes with no extra payload, rather than failing

### Requirement: Room server login body parsing
The system SHALL parse a decrypted ANON_REQ body addressed to a room server as a 4-byte
little-endian sender timestamp, a 4-byte little-endian sync timestamp, and the password as
the remainder with block padding stripped.

#### Scenario: Room login body
- **WHEN** a decrypted room-server ANON_REQ body is parsed
- **THEN** the parse yields the sender timestamp, the sync timestamp and the password text, with trailing zero padding removed

#### Scenario: Empty password
- **WHEN** a decrypted room-server ANON_REQ body contains only the two timestamps followed by padding
- **THEN** the parse yields an empty password rather than failing

### Requirement: Trace payload preservation
The system SHALL parse a TRACE payload sufficiently to preserve its bytes and identify it in
logs and tests, without being required to interpret its per-hop SNR contents in this
milestone.

#### Scenario: Trace packet in the capture corpus
- **WHEN** a TRACE packet in the capture corpus is decoded
- **THEN** the payload is preserved intact and identified as a trace, and decoding the corpus does not fail on it

### Requirement: Unsupported payload types are preserved, not dropped
The system SHALL represent MULTIPART, CONTROL, RAW_CUSTOM and reserved payload types as a
recognized-but-unparsed payload carrying the raw bytes, rather than raising or discarding
them.

#### Scenario: Multipart payload encountered
- **WHEN** a packet with payload type `0x0A` (MULTIPART) is decoded
- **THEN** the result reports the payload type and preserves the raw payload bytes without attempting to reassemble the sequence, which is an explicit v1 non-goal

### Requirement: Payload building
The system SHALL build the wire bytes for ADVERT, ACK, the REQ/RESPONSE/TXT_MSG/PATH
envelope, ANON_REQ, GRP_TXT and GRP_DATA payloads, and for the plaintext bodies those
envelopes carry, applying the same field layouts and limits it enforces on parse.

#### Scenario: Build round-trips a parsed payload
- **WHEN** any parseable payload from the capture corpus is parsed and then rebuilt
- **THEN** the resulting bytes are byte-for-byte identical to the original payload

#### Scenario: Advert appdata exceeds its limit
- **WHEN** an advert is built with appdata longer than 32 bytes (`MAX_ADVERT_DATA_SIZE`)
- **THEN** building fails with an error naming the limit rather than emitting an advert the firmware would reject
