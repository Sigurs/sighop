# payload-codec Specification

## Purpose
Parsing and building each MeshCore payload type — adverts and their appdata, encrypted
envelopes, text and group messages, acknowledgements, returned paths, traces — while preserving
uninterpreted anything it does not understand, so an unsupported type is carried rather than
dropped.
## Requirements

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
The system SHALL parse an ACK payload as a 4-byte checksum (the truncated SHA-256 defined in
`mesh-crypto`, not a CRC32) optionally followed by a 2-byte tail — an extended attempt byte
and a random byte, which current firmware appends to make the packet hash unique
(`BaseChatMesh.cpp:245-247`, `ack_hash[6]`). It SHALL accept payload lengths of exactly 4 or
exactly 6 bytes, preserve the tail when present, and reject any other length.

> Both lengths are live on the captured mesh: of the corpus's 42 ACK frames, 31 carry 4 bytes
> and 11 carry 6. An earlier reading of this requirement demanded exactly 4 bytes, which would
> have rejected those 11 frames as malformed.

#### Scenario: ACK from the capture corpus
- **WHEN** any ACK packet in the capture corpus is parsed
- **THEN** the parse yields one 4-byte checksum value, consumes the whole payload, and reports whether a 2-byte tail was present

#### Scenario: ACK of an unexpected length
- **WHEN** an ACK payload is 5 bytes long
- **THEN** the parse fails as malformed, naming the length rather than truncating to the first four bytes

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
The system SHALL parse a decrypted PATH body as a path length byte using the **same packed
encoding as the packet header** — hop count in bits 0-5, hash size minus one in bits 6-7 —
followed by `hop_count * hash_size` path bytes, a 1-byte extra payload type whose low nibble
uses the same values as the packet header's payload type field, and the remaining bytes as the
bundled extra. The bundled extra is already-decrypted content of that type, not a further
envelope, and SHALL be preserved verbatim including any block padding.

> `Mesh.cpp:167-168` reads this byte with `hash_size = (path_len >> 6) + 1` and
> `hash_count = path_len & 63`, and validates it with `Packet::isValidPathLen` — the same
> function the packet header uses. `payloads.md` still documents it as a plain count of
> single-byte hashes, which holds only for the 1-byte case. `Mesh.cpp:172` notes the extra
> "may be padded with zeroes", and `BaseChatMesh.cpp:336` reads only the leading 4 bytes of a
> bundled ACK for exactly that reason.

#### Scenario: Returned path bundling an acknowledgement
- **WHEN** a decrypted PATH body declares extra type `0x03` (ACK) with at least four extra bytes
- **THEN** the parse yields the path hashes and an ACK checksum taken from the first four extra bytes, with the remaining extra bytes preserved rather than interpreted

#### Scenario: Returned path with multi-byte hashes
- **WHEN** a decrypted PATH body's path length byte is `0x43`
- **THEN** the parse yields 3 hops of 2-byte hashes over the following 6 bytes, not 0x43 single-byte hashes

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

### Requirement: Room server login response body
The system SHALL build and parse the body a room server returns for a successful login: a 4-byte
little-endian server timestamp, a result code, a legacy interval byte, a client-kind byte, the
member's permission byte, four bytes of blob that make the packet hash unique, and the protocol
level byte the server implements.

#### Scenario: Building a login response
- **WHEN** a login response body is built for an administrator
- **THEN** the bytes carry the server timestamp, the success result, the administrator indication, the permission byte and the protocol level, in that layout

#### Scenario: Parsing a login response
- **WHEN** a login response body is parsed
- **THEN** it yields the server timestamp, result, client kind, permissions and protocol level, and a body shorter than the layout is reported as truncated rather than partially interpreted

### Requirement: Request bodies carry a request type after a timestamp
The system SHALL parse and build the body of a `REQ` payload as a 4-byte little-endian sender
timestamp followed by a request-type byte and that type's arguments, and SHALL preserve the
arguments of a type it does not interpret rather than discarding them.

#### Scenario: A keep-alive request with a position
- **WHEN** a keep-alive request body carrying a 4-byte position is parsed
- **THEN** it yields the sender timestamp, the keep-alive type and the position

#### Scenario: A keep-alive request without a position
- **WHEN** a keep-alive request body carries no position
- **THEN** it yields the sender timestamp and the type with no position, rather than reading past the body or failing

#### Scenario: An uninterpreted request type
- **WHEN** a request body carries a type the codec does not interpret
- **THEN** the type and its remaining bytes are preserved for the caller to report

### Requirement: Room server statistics body
The system SHALL build and parse the room server's statistics body in the layout the reference
room-server implementation defines — an echoed 4-byte sender timestamp followed by fifty-two bytes
of little-endian fields, four 16-bit, eight 32-bit and six 16-bit, ending in the posted and pushed
counters — and SHALL document that the last four of those bytes are the posted and pushed counters
in a room server where a repeater carries a receive-airtime value, because a generic client parser
reads them as the latter.

#### Scenario: Building the statistics body
- **WHEN** a statistics body is built
- **THEN** it is fifty-six bytes long, the first four echo the request's timestamp, and every field is little-endian at the offset the reference implementation writes it to

#### Scenario: Round-tripping the statistics body
- **WHEN** a statistics body is built and parsed back
- **THEN** every field returns the value it was given

### Requirement: Telemetry frames are built in CayenneLPP form, big-endian
The system SHALL build a telemetry frame as a sequence of entries, each a channel byte, a type byte
and that type's value **encoded most significant byte first**, and SHALL support at minimum voltage
in hundredths of a volt as an unsigned 16-bit value and temperature in tenths of a degree Celsius
as a signed 16-bit value.

#### Scenario: A voltage entry
- **WHEN** a telemetry frame carrying a voltage on the device's own channel is built
- **THEN** the entry is the channel byte, the voltage type byte, and the hundredths-of-a-volt value most significant byte first

#### Scenario: A negative temperature
- **WHEN** a telemetry frame carrying a temperature below zero is built
- **THEN** the value is encoded as a signed 16-bit quantity in tenths of a degree, most significant byte first

#### Scenario: An empty frame
- **WHEN** a telemetry frame is built with no values
- **THEN** it is empty rather than carrying a placeholder entry

### Requirement: Trace payload preservation
The system SHALL parse a TRACE payload sufficiently to preserve its bytes and identify it in
logs and tests, without being required to interpret its per-hop SNR contents in this
milestone.

#### Scenario: Trace packet in the capture corpus
- **WHEN** a TRACE packet in the capture corpus is decoded
- **THEN** the payload is preserved intact and identified as a trace, and decoding the corpus does not fail on it

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

### Requirement: Group text body building
The system SHALL build a group text body from a timestamp, a sender name and a message body as the
plain-text-message layout with text type plain, attempt zero, and text `<sender name>: <body>`, and
SHALL refuse to build one whose sender name contains `": "`, because a receiver splits on the first
occurrence.

#### Scenario: Build round-trips a parse
- **WHEN** a group text body is built with sender `dev-companion` and body `hello: world` and then parsed
- **THEN** the parse reports sender name `dev-companion`, body `hello: world`, text type plain and attempt zero

#### Scenario: A sender name containing the separator
- **WHEN** a group text body is built with sender name `a: b`
- **THEN** building fails naming the separator

#### Scenario: Corpus group text rebuilds
- **WHEN** a decrypted plain group text body from the capture corpus is parsed and rebuilt from its timestamp, sender name and body
- **THEN** the rebuilt bytes equal the decrypted bytes with their zero padding removed

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
