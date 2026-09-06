# entity-store Specification

## Purpose
How local entity identities are held in the database with their private keys encrypted at rest,
so that a database dump is not sufficient to impersonate a room server, and how identities move
between a keyfile and that store.

## Requirements

### Requirement: A private key is never stored in the clear
The system SHALL encrypt an entity's private seed before it reaches the database, using a secret
supplied by the environment and held nowhere in the database, and SHALL store no representation
from which the seed can be recovered without that secret. A stored key SHALL carry an
authentication tag, so tampering with the stored value is detected rather than yielding a
different key.

#### Scenario: Storing an identity
- **WHEN** an entity identity is written to the database
- **THEN** the stored key material is ciphertext, the plaintext seed appears in no column, and the row alone is insufficient to reconstruct the identity

#### Scenario: Stored key material is altered
- **WHEN** stored key material is modified and then loaded
- **THEN** loading fails with an authentication error naming the entity, and no key is produced

#### Scenario: Loaded identity matches the stored public key
- **WHEN** an entity is loaded and its seed decrypted
- **THEN** the public key derived from that seed is compared against the public key stored beside it, and a mismatch is an error naming the entity rather than a silently preferred value

### Requirement: The encryption secret is required, validated, and never derived from a passphrase
The system SHALL require the encryption secret to be present and of the exact expected length
whenever entity identities are read from or written to the database, SHALL fail at startup with a
message naming the missing or malformed variable, and SHALL NOT derive the secret from a
passphrase or generate one on the fly. The system SHALL provide a way to generate a
correctly-formed secret.

#### Scenario: Secret missing
- **WHEN** the runtime starts with a database configured, entities persisted, and no encryption secret in the environment
- **THEN** startup fails naming the environment variable, and no entity is loaded

#### Scenario: Secret malformed
- **WHEN** the supplied secret is not of the expected length or encoding
- **THEN** startup fails saying which it is, and does not pad, truncate or hash the value into shape

#### Scenario: Secret does not open the stored keys
- **WHEN** a secret of the correct form does not decrypt an existing entity
- **THEN** the failure is reported as a wrong-secret error naming the entity, distinct from a corrupt-data error

#### Scenario: Generating a secret
- **WHEN** the operator asks for a new encryption secret
- **THEN** a correctly-formed secret is printed once, together with the statement that losing it makes every stored identity unrecoverable

### Requirement: Persisted entities carry their identity and advert configuration
The system SHALL store, for each local entity, a stable identifier, the entity type, the name,
the public key, the node hash, the encrypted seed, its advert configuration, and whether it is
enabled; and SHALL restore all of it on the next run so that a public key published to another
node stays valid and adverts resume on their configured schedule.

#### Scenario: Restart with persisted entities
- **WHEN** the runtime restarts against a database holding entities
- **THEN** each enabled entity is loaded with the same public key and node hash it had before, its advert configuration is restored, and the loaded entities are reported at startup

#### Scenario: Disabled entity
- **WHEN** a persisted entity is marked disabled
- **THEN** it is not loaded as an originating identity, it adverts nothing, and its presence is still reported

### Requirement: Node hashes do not collide across persisted and loaded entities
The system SHALL apply the node-hash collision rule across every local entity in one run,
whichever source it came from, SHALL refuse to start when two of them share a node hash, naming
both, and SHALL reject a newly generated keypair that collides with any of them.

#### Scenario: A new entity collides with a persisted one
- **WHEN** an entity is created while a persisted entity with the same node hash exists
- **THEN** the keypair is discarded and generation retries until a non-colliding node hash is found

#### Scenario: Two persisted entities collide
- **WHEN** the database holds two enabled entities whose public keys share their first byte
- **THEN** startup fails naming both entities and their shared node hash

### Requirement: Identities can be imported and exported deliberately
The system SHALL provide an explicit action to import an identity into the store from a keyfile
and an explicit action to export a stored identity back to one, so an operator can back an
identity up on purpose. Export SHALL be a distinct command from inspection, SHALL state that the
written file contains private key material, and SHALL write it with owner-only permissions.

#### Scenario: Importing a keyfile
- **WHEN** an identity keyfile is imported
- **THEN** an entity row is created with the seed encrypted, the same public key and node hash, and the operation reports the public key it stored

#### Scenario: Importing an identity already stored
- **WHEN** a keyfile whose public key already exists in the store is imported
- **THEN** the import fails naming the existing entity, and the stored row is unchanged

#### Scenario: Exporting an identity
- **WHEN** a stored identity is exported to a path
- **THEN** a keyfile readable by the owner only is written, the output states that it holds private key material, and the stored row is unchanged

#### Scenario: Export targets an existing file
- **WHEN** export is asked to write to a path that already exists
- **THEN** it fails naming the path, and the file's contents are unchanged

### Requirement: Inspection never discloses key material
The system SHALL provide listing and inspection of stored entities — name, type, public key, node
hash, advert configuration, enabled state — that discloses neither the seed nor its ciphertext,
and SHALL keep key material out of log events, error messages and status output.

#### Scenario: Listing stored entities
- **WHEN** stored entities are listed
- **THEN** each is shown with its name, type, public key and node hash, and neither the seed nor the stored ciphertext appears

#### Scenario: An entity operation fails
- **WHEN** an entity operation fails and is logged
- **THEN** the event names the entity and the failure, and contains no key material and no encryption secret

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
