## ADDED Requirements

> Reference: DESIGN.md §4.2 (reverse path learning), §5 (a MAC match selects a key and never
> authenticates a sender). The reference implementation sends an explicit route in a `PATH` payload
> and bundles an acknowledgement inside it (`examples/simple_room_server/MyMesh.cpp:601-620`); a
> route learned only from the frame a reception arrived on misses both.

### Requirement: An explicit path body is a source of routes, and its bundled payload is not discarded
The system SHALL learn a route from a decrypted `PATH` body addressed to a local entity, recording
it as a candidate for the sender the decryption key identifies, and SHALL deliver any payload
bundled inside that body for handling as though it had arrived on its own. The decryption SHALL use
the same candidate-key trial as an encrypted text message.

#### Scenario: A peer returns an explicit path
- **WHEN** a `PATH` payload addressed to a local entity is decrypted
- **THEN** the route it declares is recorded as a candidate for the sender whose key decrypted it, alongside any route already learned from the frame it arrived on

#### Scenario: An acknowledgement bundled in a path return
- **WHEN** a decrypted `PATH` body carries a bundled acknowledgement
- **THEN** that acknowledgement is handled exactly as one that arrived as its own packet, so a delivery answered this way is resolved rather than retried

#### Scenario: No candidate key decrypts the path body
- **WHEN** no candidate key verifies the MAC of a `PATH` payload
- **THEN** no route is recorded, nothing is discarded silently, and the reception is reported with the number of candidates tried

### Requirement: A route learned from a path body is claimed, not proven
The system SHALL record a route learned from a decrypted path body with the same standing as any
other candidate route: keyed by the public key whose shared secret decrypted it, marked as claimed
rather than authenticated, and subject to the same most-recently-confirmed-wins selection and the
same candidate limits. A path body SHALL NOT be treated as evidence of the sender's identity.

#### Scenario: A path body and a reverse path disagree
- **WHEN** a route learned from a decrypted path body differs from one learned by reversing a reception's own path
- **THEN** both are retained as candidates and the most recently confirmed one is selected, exactly as for two reverse-learned candidates

#### Scenario: Rendering a route learned this way
- **WHEN** a route learned from a decrypted path body is reported
- **THEN** its sender is presented as claimed, in the same visual convention the runtime uses for other unverified content
