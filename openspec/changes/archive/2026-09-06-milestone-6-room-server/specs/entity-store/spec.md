## ADDED Requirements

> Reference: DESIGN.md §7 (*"a distinct admin password per room server entity, not one
> platform-wide"* — each virtual room server is a separate identity to the mesh), §5 (the node type
> is an enum in the appdata flags byte, not a bit flag), §6 (the entity table and its advert
> configuration).

### Requirement: An entity can be created as a room server
The system SHALL allow a stored entity to be created as a room server, recording it with the
room-server entity type and with the room-server node type in its advert configuration, so that its
adverts identify it to the mesh as a room server without any further configuration. The choice
SHALL be explicit at creation; an entity SHALL NOT become a room server as a side effect of having
a room bound to it.

#### Scenario: Creating a room server identity
- **WHEN** an identity is created as a room server
- **THEN** it is stored with the room-server entity type, its advert configuration carries the room-server node type, and inspecting it reports both

#### Scenario: Creating an ordinary identity
- **WHEN** an identity is created without a type given
- **THEN** it is stored exactly as before this capability changed, as an ordinary chat identity

#### Scenario: An imported keyfile declaring a node type
- **WHEN** a keyfile declaring the room-server node type is imported
- **THEN** the stored entity carries that node type and the room-server entity type, rather than being coerced to the default

### Requirement: A room server identity's private key is protected exactly as any other
The system SHALL seal a room server identity's seed under the same environment secret, with the
same refusal to start on a missing or malformed secret, and SHALL apply the same node-hash
collision rule across room server and ordinary entities, so that being a room server changes
nothing about how the identity is stored or checked.

#### Scenario: A room server seed at rest
- **WHEN** a room server identity is stored
- **THEN** its seed is sealed exactly as any other entity's, and a database dump yields no usable key

#### Scenario: A room server colliding with an ordinary entity
- **WHEN** a room server identity and another entity share a node hash
- **THEN** startup fails naming both and the shared hash, exactly as for two ordinary entities
