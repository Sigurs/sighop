## ADDED Requirements

> Reference: the bodies this milestone puts on the wire, taken from
> `examples/simple_room_server/MyMesh.cpp:20-39`, `:151-217` and `:382-401`, and from the CayenneLPP
> encoding the firmware's telemetry helper produces. Parsing and building stay pure functions over
> bytes: nothing here learns about rooms, members or storage.

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

## MODIFIED Requirements

### Requirement: Payload building
The system SHALL build the wire bytes for ADVERT, ACK, the REQ/RESPONSE/TXT_MSG/PATH
envelope, ANON_REQ, GRP_TXT and GRP_DATA payloads, and for the plaintext bodies those
envelopes carry, applying the same field layouts and limits it enforces on parse. Building a
returned path body SHALL accept a bundled extra payload of a stated type and emit it verbatim after
the path hashes, so that a reply carrying both a route and an answer inside it can be composed.

#### Scenario: Build round-trips a parsed payload
- **WHEN** any parseable payload from the capture corpus is parsed and then rebuilt
- **THEN** the resulting bytes are byte-for-byte identical to the original payload

#### Scenario: Advert appdata exceeds its limit
- **WHEN** an advert is built with appdata longer than 32 bytes (`MAX_ADVERT_DATA_SIZE`)
- **THEN** building fails with an error naming the limit rather than emitting an advert the firmware would reject

#### Scenario: A returned path bundling a response
- **WHEN** a returned path body is built with a path and a bundled payload of a stated type
- **THEN** the bytes carry the packed hop-count and hash-size byte, the path hashes, the extra type byte and the bundled payload unchanged, and parsing them yields what was supplied
