## ADDED Requirements

> Reference: milestone 7 gives an identity a second role. A bot is a companion to every other node
> on the mesh — it is a chat node on the wire — so only the stored type distinguishes it, and
> nothing about sealing, collision checking or export changes.

### Requirement: An entity can be created as a bot
The system SHALL allow a stored entity to be created as a bot, recording it with the bot entity
type and with the ordinary chat node type in its advert configuration, because a bot presents
itself to the mesh as a chat node and its automation is sighop's business rather than the mesh's.
The choice SHALL be explicit at creation; an entity SHALL NOT become a bot as a side effect of
having a bot bound to it.

#### Scenario: Creating a bot identity
- **WHEN** an identity is created as a bot
- **THEN** it is stored with the bot entity type, its advert configuration carries the chat node type, and inspecting it reports both

#### Scenario: A bot identity on the wire
- **WHEN** a bot identity adverts
- **THEN** the advert declares the chat node type, indistinguishable from a companion's

#### Scenario: An entity that is already a room server
- **WHEN** a bot is bound to an entity stored as a room server
- **THEN** the binding is refused and says the entity already has a role

### Requirement: A bot identity's private key is protected exactly as any other
The system SHALL seal a bot identity's seed under the same environment secret, with the same
refusal to start on a missing or malformed secret, and SHALL apply the same node-hash collision
rule across bot, room server and ordinary entities, so that being a bot changes nothing about how
the identity is stored or checked.

#### Scenario: A bot seed at rest
- **WHEN** a bot identity is stored
- **THEN** its seed is sealed exactly as any other entity's, and a database dump alone does not disclose it

#### Scenario: A bot identity colliding with an existing node hash
- **WHEN** a bot identity is created whose node hash collides with an existing entity's
- **THEN** it is refused by the same rule that governs any other entity
