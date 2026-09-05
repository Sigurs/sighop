# local-identity Specification

## Purpose
TBD - created by archiving change milestone-4-first-transmit. Update Purpose after archive.
## Requirements
### Requirement: An entity identity survives the process
The system SHALL be able to store a local entity's identity and reload it on a later run, so
that a public key published to another node stays valid across restarts. The system SHALL
support two stores for this: a keyfile, and — where a database is configured — the entity store,
which holds the same identity with its seed encrypted at rest. A keyfile SHALL record at minimum
the 32-byte seed, the derived public key, the entity name and the node type, and SHALL be
readable by a later version through a documented format. Where both stores hold an identity for
one run, the system SHALL state which one each loaded entity came from rather than leaving the
source to be inferred.

#### Scenario: Keyfile absent
- **WHEN** an entity is requested with a keyfile path that does not exist and generation is requested
- **THEN** a new keypair is generated, written to that path, and the same identity is loaded

#### Scenario: Keyfile present
- **WHEN** an entity is loaded from an existing keyfile
- **THEN** the public key and node hash are identical to those of the run that created it

#### Scenario: Public key stored in the file matches the seed
- **WHEN** a keyfile is loaded
- **THEN** the public key derived from the stored seed is compared against the stored public key, and a mismatch is an error naming the file rather than a silently preferred value

#### Scenario: Identity held in the entity store
- **WHEN** an entity is loaded from the entity store on a run with a database configured
- **THEN** the public key and node hash are identical to those it was stored with, and the startup output names the entity store as its source

#### Scenario: One run loads identities from both sources
- **WHEN** a run loads one identity from a keyfile and another from the entity store
- **THEN** both are usable local entities, each is reported with the source it came from, and the node-hash collision rule applies across both

### Requirement: A keyfile is an interchange format, not the store of record
Where a database is configured, the system SHALL treat the entity store as the store of record
for a local identity and a keyfile as a means of moving one in or out. The system SHALL NOT
write an identity's seed to a keyfile as a side effect of ordinary operation, and SHALL require a
distinct, explicit action to produce one.

#### Scenario: Ordinary run with persisted entities
- **WHEN** the runtime runs with a database configured and entities in the entity store
- **THEN** no keyfile is written and no seed reaches the filesystem

#### Scenario: Creating an identity with a database configured
- **WHEN** an identity is created with a database configured and no keyfile path requested
- **THEN** the identity is written to the entity store with its seed encrypted, and no keyfile is produced

#### Scenario: Producing a keyfile from a stored identity
- **WHEN** an export of a stored identity is explicitly requested
- **THEN** a keyfile is written with owner-only permissions and the operation states that the file holds private key material

### Requirement: A keyfile holds a seed in the clear and says so
The system SHALL state, wherever a keyfile is created or exported, that the file contains an
unencrypted private seed and is protected only by its filesystem permissions — because the same
seed inside the entity store is encrypted, and an operator must not conclude that the two offer
the same protection.

#### Scenario: Creating a keyfile
- **WHEN** a keyfile is created
- **THEN** the output states that the file holds an unencrypted seed protected only by its permissions

#### Scenario: Exporting to a keyfile
- **WHEN** a stored identity is exported to a keyfile
- **THEN** the output states the same, naming the path written

### Requirement: An existing keyfile is never silently overwritten
The system SHALL refuse to write over an existing keyfile, and SHALL fail with an error naming
the path rather than replacing an identity another node may already hold.

#### Scenario: Key creation targets an existing file
- **WHEN** identity creation is asked to write to a path that already exists
- **THEN** the operation fails with an error naming the path, and the file's contents are unchanged

### Requirement: Keyfiles are written with owner-only permissions
The system SHALL create keyfiles with filesystem permissions granting access to the owner only
(`0600`), and SHALL report a warning when loading a keyfile whose permissions are broader.

#### Scenario: Newly created keyfile
- **WHEN** a keyfile is created
- **THEN** its permissions are `0600`

#### Scenario: Loading a world-readable keyfile
- **WHEN** a keyfile readable by other users is loaded
- **THEN** the identity loads and a warning naming the path and its mode is emitted

### Requirement: Local entity node hashes do not collide
The system SHALL reject a generated keypair whose node hash collides with that of an already
loaded local entity and generate another, and SHALL refuse to load two local entities that
share a node hash, reporting which files conflict.

#### Scenario: Generation collides with a loaded entity
- **WHEN** a keypair is generated while an entity with the same node hash is loaded
- **THEN** the keypair is discarded and generation retries until a non-colliding node hash is found

#### Scenario: Two keyfiles collide
- **WHEN** two keyfiles whose public keys share a first byte are loaded in one run
- **THEN** startup fails with an error naming both files and their shared node hash

### Requirement: The seed is never printed by inspection commands
The system SHALL provide a way to inspect an identity — name, public key, node type and node
hash — without disclosing the seed, and SHALL require a distinct, explicit action to export
private key material.

#### Scenario: Inspecting an identity
- **WHEN** an identity is inspected through the command-line surface
- **THEN** the output carries the name, public key, node type and node hash, and does not contain the seed

### Requirement: A committed test identity is marked as burned
The system SHALL mark any identity keyfile committed to the repository as a published test
vector that must never be used on air again, both inside the file and at the point the test
suite loads it.

#### Scenario: Test fixture keyfile
- **WHEN** a keyfile is committed as a test fixture for the recorded peer exchange
- **THEN** the file carries an explicit marker identifying it as burned and unusable for a real entity, and the test reading it states the same

