# rx-decode Specification

## Purpose
The stateless stage that composes packet, payload and crypto decoding into one structured record
per received frame: every frame yields an outcome, adverts are verified before their content is
exposed, and an encrypted payload we hold no key for is reported as un-openable rather than as a
failure.
## Requirements

> Reference: DESIGN.md §4.2 (RX pipeline), §5 (advert verification), §9 (the *Packet RX* wide
> event). The `packet-codec`, `payload-codec` and `mesh-crypto` capabilities define the decoding
> this stage composes; it adds no wire-format rules of its own.

### Requirement: Modem RX events are decoded into structured records
The system SHALL decode each modem RX event into a record carrying the packet header fields,
the route type, the path, the parsed payload, and the correlated SNR and RSSI when present,
by composing the existing packet and payload codecs without reimplementing them.

#### Scenario: A well-formed packet is received
- **WHEN** the modem emits an RX event whose bytes decode as a valid packet with a parseable payload
- **THEN** the pipeline produces a decoded record carrying the header fields, route type, hop count, path bytes, hash size, parsed payload and the RxMeta values

#### Scenario: RX event with no correlated RxMeta
- **WHEN** the modem emits an RX event with no RxMeta attached
- **THEN** the decoded record carries the SNR and RSSI as explicitly absent rather than as zero or a default

### Requirement: Every frame produces an outcome
The system SHALL produce a structured outcome for every event it consumes — including frames
that fail structural decode, frames whose payload cannot be parsed, and unparsed-frame events
forwarded by the modem — and SHALL NOT discard any event silently.

#### Scenario: Structural decode fails
- **WHEN** an RX event's bytes violate a packet-level limit or are truncated
- **THEN** the pipeline produces a failure outcome carrying the violated rule, the offset and the raw bytes, and continues with the next event

#### Scenario: Payload parse fails on a structurally valid packet
- **WHEN** a packet decodes structurally but its payload body cannot be parsed
- **THEN** the pipeline produces a record carrying the decoded packet together with the payload failure, so the header and path remain observable

#### Scenario: Payload type is one the system does not interpret
- **WHEN** a packet carries a payload type the codec preserves but does not interpret
- **THEN** the pipeline produces a record marking the payload as uninterpreted and carrying its raw bytes, which is an outcome and not a failure

#### Scenario: Modem forwards an unparsed frame
- **WHEN** the modem emits an unparsed-frame event
- **THEN** the pipeline produces a corresponding outcome carrying the raw bytes and the modem's stated reason

### Requirement: Adverts are verified before their content is exposed
The system SHALL verify an advert's Ed25519 signature before exposing any advert content, and
SHALL expose the verification outcome as part of the record such that a consumer cannot obtain
the advert's name, node type or location without also obtaining the verification result.

#### Scenario: Advert with a valid signature
- **WHEN** an ADVERT payload's signature verifies against the public key it carries
- **THEN** the record carries a verified advert including its name, node type, flags and location

#### Scenario: Advert with an invalid signature
- **WHEN** an ADVERT payload's signature does not verify
- **THEN** the record carries a verification failure and does not expose the advert's name as verified content

### Requirement: Encrypted payloads are reported as un-openable, not as failures
The system SHALL treat an encrypted payload for which no key is held as a normal outcome
carrying the envelope fields, and SHALL NOT report it as a decode failure.

#### Scenario: Encrypted direct message addressed to a third party
- **WHEN** a TXT_MSG, REQ, RESPONSE or PATH payload is received and no key is held for it
- **THEN** the record carries the destination hash, source hash, MAC and ciphertext length, marked as not decrypted, with no failure recorded

### Requirement: Reception identity
The system SHALL mint an identifier for each received frame at ingress and carry it through the
decoded record and every log event derived from that frame, and this identifier SHALL identify
the reception rather than the packet's content, so that two receptions of identical bytes carry
different identifiers.

#### Scenario: The same packet is received twice
- **WHEN** two RX events carry identical packet bytes
- **THEN** each produces a record with its own distinct reception identifier

### Requirement: Packet RX wide event
The system SHALL emit one wide event per received frame containing at minimum the reception
identifier, route type, payload type, hop count, path, size in bytes, SNR, RSSI, source hash and
the decode outcome, per DESIGN.md §9.

#### Scenario: A frame is decoded
- **WHEN** the pipeline finishes processing an RX event
- **THEN** exactly one *Packet RX* wide event is emitted for it, carrying the fields above and the outcome of decoding

#### Scenario: A frame fails to decode
- **WHEN** the pipeline processes an RX event whose bytes fail structural decode
- **THEN** a *Packet RX* wide event is still emitted, carrying the reception identifier, the raw size, the signal values and the failure reason

### Requirement: The decode stage is stateless
The system SHALL NOT deduplicate, learn paths, accumulate contacts or persist anything as part
of decoding; each event is processed independently of every other.

#### Scenario: Duplicate receptions
- **WHEN** the same packet is received several times by flood repetition
- **THEN** each reception is decoded and reported independently, with no suppression of repeats
