# Spec Delta

## MODIFIED Requirements

### Requirement: Persisted entities carry their identity and advert configuration
The system SHALL store, for each local entity, a stable identifier, the entity type, the name,
the public key, the node hash, the encrypted private key, its advert configuration, and whether it
is enabled; and SHALL restore all of it on the next run so that a public key published to another
node stays valid and adverts resume on their configured schedule.

#### Scenario: Restart with persisted entities
- **WHEN** the runtime restarts against a database holding entities
- **THEN** each enabled entity is loaded with the same public key and node hash it had before, its advert configuration is restored, and the loaded entities are reported at startup

#### Scenario: Disabled entity
- **WHEN** a persisted entity is marked disabled
- **THEN** it is not loaded as an originating identity, it adverts nothing, and its presence is still reported

### Requirement: Inspection never discloses key material
The system SHALL provide listing and inspection of stored entities — name, type, public key, node
hash, advert configuration, enabled state — that discloses neither the private key nor its
ciphertext, and SHALL keep key material out of log events, error messages and status output.

#### Scenario: Listing stored entities
- **WHEN** stored entities are listed
- **THEN** each is shown with its name, type, public key and node hash, and neither the private key nor the stored ciphertext appears

#### Scenario: An entity operation fails
- **WHEN** an entity operation fails and is logged
- **THEN** the event names the entity and the failure, and contains no key material and no encryption secret

### Requirement: A room server identity's private key is protected exactly as any other
The system SHALL seal a room server identity's private key under the same environment secret, with
the same refusal to start on a missing or malformed secret, and SHALL apply the same node-hash
collision rule across room server and ordinary entities, so that being a room server changes
nothing about how the identity is stored or checked.

#### Scenario: A room server seed at rest
- **WHEN** a room server identity is stored
- **THEN** its private key is sealed exactly as any other entity's, and a database dump yields no usable key

#### Scenario: A room server colliding with an ordinary entity
- **WHEN** a room server identity and another entity share a node hash
- **THEN** startup fails naming both and the shared hash, exactly as for two ordinary entities

### Requirement: A bot identity's private key is protected exactly as any other
The system SHALL seal a bot identity's private key under the same environment secret, with the
same refusal to start on a missing or malformed secret, and SHALL apply the same node-hash
collision rule across bot, room server and ordinary entities, so that being a bot changes nothing
about how the identity is stored or checked.

#### Scenario: A bot seed at rest
- **WHEN** a bot identity is stored
- **THEN** its private key is sealed exactly as any other entity's, and a database dump alone does not disclose it

#### Scenario: A bot identity colliding with an existing node hash
- **WHEN** a bot identity is created whose node hash collides with an existing entity's
- **THEN** it is refused by the same rule that governs any other entity
