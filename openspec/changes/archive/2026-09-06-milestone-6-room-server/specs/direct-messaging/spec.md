## MODIFIED Requirements

> Reference: milestone 6 adds a second component that waits on acknowledgements and a second
> component that decrypts traffic addressed to a local entity. Neither may make the other's
> reporting wrong. Nothing about composition, routing, retries, timeouts or the claimed-sender rule
> changes.

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
