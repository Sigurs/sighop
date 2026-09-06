# direct-messaging Specification

## Purpose
TBD - created by archiving change milestone-4-first-transmit. Update Purpose after archive.
## Requirements
### Requirement: Outbound text message composition
The system SHALL compose a direct text message as the plaintext `timestamp` (4 bytes,
little-endian) ‖ `flags` (1 byte) ‖ `text`, where `flags` is `(attempt & 0x03) | (txt_type << 2)`,
and SHALL NOT transmit a trailing NUL terminator. The plaintext SHALL be encrypted-then-MAC'd to
the shared secret of (sending entity, recipient contact) and carried in a `TXT_MSG` envelope
addressed with the recipient's node hash as destination and the sending entity's node hash as
source.

#### Scenario: A message is composed
- **WHEN** a text message is composed for a contact
- **THEN** the envelope carries the recipient's node hash, the sender's node hash, the 2-byte MAC and the ciphertext, and the ciphertext length is a positive multiple of 16

#### Scenario: The NUL terminator is not transmitted
- **WHEN** a message of `n` text bytes is composed
- **THEN** the plaintext hashed for the expected acknowledgement and the plaintext encrypted are both exactly `5 + n` bytes

#### Scenario: Text exceeds the protocol maximum
- **WHEN** a message longer than the firmware's maximum text length is submitted
- **THEN** composition fails with an error naming the limit, and nothing is queued for transmission

### Requirement: Routing prefers a known zero-hop route and never floods by default
The system SHALL send a direct message as `DIRECT` using a learned route to the recipient when
one is known, SHALL send it with an empty path when the learned route is zero-hop, and SHALL
refuse to send when no route is known unless flooding has been explicitly permitted for that
invocation.

#### Scenario: A zero-hop route is known
- **WHEN** a message is sent to a contact for which a zero-hop route was learned
- **THEN** the packet is `DIRECT` with an empty path

#### Scenario: No route is known and flooding was not permitted
- **WHEN** a message is sent to a contact with no learned route and no explicit flood permission
- **THEN** the send fails with an error saying no route is known, and no packet is queued

#### Scenario: No route is known and flooding was permitted
- **WHEN** a message is sent to a contact with no learned route and flooding explicitly permitted
- **THEN** the packet is sent `FLOOD`, and the output states that it was flooded

### Requirement: Outbound messages are class 2 and acknowledgements are class 0
The system SHALL submit originated direct messages at priority class 2 and acknowledgements it
emits at priority class 0, and SHALL set each message submission's deadline to its
acknowledgement timeout so a message that cannot reach the air within its own retry window is
dropped and logged rather than sent late.

#### Scenario: Message and acknowledgement priorities
- **WHEN** a direct message is submitted and an acknowledgement is emitted
- **THEN** the message is submitted at class 2 and the acknowledgement at class 0

#### Scenario: A message misses its deadline in the queue
- **WHEN** a queued message's acknowledgement timeout elapses before it reaches the hand-off
- **THEN** it is dropped, logged as dropped with its queue wait, and reported to the caller as undelivered

### Requirement: The expected acknowledgement is computed as the firmware computes it
The system SHALL compute the acknowledgement it expects for a sent message as the **first 4
bytes** of `sha256(timestamp ‖ flags ‖ text ‖ sender public key)`, over exactly the transmitted
plaintext, and SHALL compute the acknowledgement it emits for a received message as the same
hash over the received plaintext with the **sender's** public key.

#### Scenario: Expected acknowledgement for a sent message
- **WHEN** a message is composed
- **THEN** its expected acknowledgement is the first 4 bytes of the SHA-256 of the transmitted plaintext followed by the sending entity's public key

#### Scenario: Acknowledgement emitted for a received message
- **WHEN** a direct message is successfully decrypted
- **THEN** the emitted acknowledgement is the first 4 bytes of the SHA-256 of the received plaintext followed by the sending contact's public key

### Requirement: Acknowledgement payloads are matched on their first four bytes
The system SHALL accept an acknowledgement payload of 4 or 6 bytes and SHALL compare only its
first 4 bytes against outstanding expectations, because the reference implementation appends an
extended attempt byte and a random byte in one of its paths. Outstanding expectations SHALL be held
in one place shared by every component that waits on an acknowledgement, so that an acknowledgement
is matched against all of them and is reported as unmatched only when no component in the process
was waiting for it.

#### Scenario: Six-byte acknowledgement
- **WHEN** a 6-byte acknowledgement whose first 4 bytes match an outstanding expectation is received
- **THEN** the corresponding message is resolved as acknowledged

#### Scenario: Acknowledgement matching nothing outstanding
- **WHEN** an acknowledgement matching no outstanding expectation is received
- **THEN** it is reported and discarded, and no message state changes

#### Scenario: An acknowledgement awaited by another component
- **WHEN** an acknowledgement arrives that a component other than the direct messenger is waiting for
- **THEN** it is delivered to that component and is not reported as unmatched

#### Scenario: An acknowledgement bundled inside a returned path
- **WHEN** an acknowledgement arrives bundled inside a decrypted returned-path body
- **THEN** it is matched against outstanding expectations exactly as a standalone acknowledgement is

### Requirement: Retries reuse the timestamp, increment the attempt, and are bounded
The system SHALL retry an unacknowledged message with the **same timestamp** and an incremented
attempt counter, SHALL cap the attempt counter at 3 so that the reference implementation's
extended-attempt encoding is never produced, and SHALL track the expected acknowledgement of
every attempt made, accepting a match against any of them.

#### Scenario: First attempt goes unacknowledged
- **WHEN** the acknowledgement timeout elapses with no match
- **THEN** the message is resubmitted with the same timestamp, the attempt incremented, and a different ciphertext

#### Scenario: A late acknowledgement for an earlier attempt
- **WHEN** an acknowledgement matching the expectation of an earlier attempt arrives after a later attempt was sent
- **THEN** the message is resolved as acknowledged

#### Scenario: Attempts are exhausted
- **WHEN** four attempts (0 through 3) have gone unacknowledged
- **THEN** the message is resolved as unacknowledged and reported as such, never silently abandoned

### Requirement: The acknowledgement timeout uses the reference implementation's formula
The system SHALL derive each acknowledgement timeout from the transmitted packet's computed
time-on-air as the reference peer does: `500 ms + 16 × airtime` for a flooded message, and
`500 ms + (6 × airtime + 250 ms) × (hops + 1)` for a direct one, where `airtime` comes from the
`airtime` capability's computation under the live radio parameters.

#### Scenario: Direct zero-hop message
- **WHEN** a zero-hop direct message with a computed time-on-air of `t` milliseconds is sent
- **THEN** its acknowledgement timeout is `500 + (6 × t + 250)` milliseconds

#### Scenario: Flooded message
- **WHEN** a flooded message with a computed time-on-air of `t` milliseconds is sent
- **THEN** its acknowledgement timeout is `500 + 16 × t` milliseconds

### Requirement: Inbound direct messages are handled behind the bus, not in the decode stage
The system SHALL handle inbound direct messages as a bus subscriber downstream of deduplication
and path learning, and SHALL NOT introduce key material or contact state into the stateless
decode stage.

#### Scenario: Decode stage remains stateless
- **WHEN** a capture containing encrypted direct messages is replayed
- **THEN** the decode stage produces the same records as before this capability existed, and decryption happens only in the subscriber

### Requirement: Inbound decryption trials candidate keys and reports what it tried
The system SHALL attempt decryption of a `TXT_MSG` envelope for every local entity whose node
hash equals the envelope's destination hash **and which is not serving a room**, against every
contact whose node hash equals the source hash — falling back to all contacts when the source hash
matches none — and SHALL verify the MAC before decrypting each candidate. Failure to match any
candidate SHALL be reported with the number of candidates tried, never silently dropped. An entity
that serves a room SHALL be excluded here because its traffic is handled by that room server, so
that exactly one component decrypts a packet and at most one acknowledgement is transmitted for it.

#### Scenario: The intended recipient is one of several matching entities
- **WHEN** an envelope's destination hash matches two local entities and one of their shared secrets verifies the MAC
- **THEN** that entity's decryption is used and the other candidate is reported as tried and rejected

#### Scenario: No candidate matches
- **WHEN** no candidate pair verifies the MAC
- **THEN** the reception is reported as an undecryptable direct message together with the candidate count, and the packet is otherwise preserved as received

#### Scenario: The destination is an entity serving a room
- **WHEN** an envelope's destination hash matches only an entity that serves a room
- **THEN** no direct-message decryption is attempted, no acknowledgement is sent from the direct messenger, and the packet is left to the room server

#### Scenario: A room server entity shares a node hash with an ordinary entity
- **WHEN** an envelope's destination hash matches both an entity serving a room and one that does not
- **THEN** the ordinary entity is still tried, and the room server entity is not tried here

### Requirement: A MAC match selects a key and never authenticates a sender
The system SHALL treat a MAC match as evidence that a key decrypts a payload, never as proof of
the sender's identity, and SHALL render a decrypted message's originator as a claimed contact,
visually distinct from a signature-verified identity.

#### Scenario: Rendering a decrypted message
- **WHEN** a decrypted direct message is rendered
- **THEN** the sender is presented as claimed rather than verified, in the same visual convention the runtime uses for other unverified content

### Requirement: A decrypted message is acknowledged and reported
The system SHALL, on successfully decrypting and parsing an inbound direct message, report the
message with its sender, timestamp, text and reception metadata, and SHALL submit an
acknowledgement addressed to the sender at priority class 0 by the same routing rules as an
outbound message.

#### Scenario: Message received and acknowledged
- **WHEN** an inbound direct message decrypts and parses
- **THEN** the message is reported and an acknowledgement is queued to its sender

#### Scenario: Message decrypts but does not parse
- **WHEN** a candidate's MAC verifies but the plaintext does not parse as a text message body
- **THEN** the failure is reported with the parse reason, and no acknowledgement is sent

