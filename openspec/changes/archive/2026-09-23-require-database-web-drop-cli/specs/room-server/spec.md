# Spec Delta

## ADDED Requirements

### Requirement: A room is bound to exactly one stored identity, which adverts as a room server entity
The system SHALL bind a room to exactly one stored entity, SHALL advertise that entity with the
room-server node type on the same advert schedule and under the same advert policy as any other
entity, and SHALL refuse to bind a second room to an identity that already has one.

#### Scenario: A room server adverts
- **WHEN** a room server entity's advert is due
- **THEN** the advert carries the room-server node type and is submitted through the transmit scheduler like any other advert, subject to the advert floor and the duty-cycle ceiling

#### Scenario: Binding a second room to one identity
- **WHEN** a room is created on an identity that already has one
- **THEN** it is refused with a message naming the existing room, and nothing is changed

## REMOVED Requirements

### Requirement: A room is bound to exactly one stored identity, which adverts as a room server
**Reason**: Its last scenario described a node started with no durable storage, which can no longer
start. The binding and advert rules are unchanged and are restated.
**Migration**: Replaced by "A room is bound to exactly one stored identity, which adverts as a room
server entity" above.
