## ADDED Requirements

> Reference: DESIGN.md §3 (virtual entity, node-hash collisions), §6 *Private keys at rest*
> (which this capability deliberately anticipates rather than implements), §12 milestone 4.
> The keypair primitives come from `mesh-crypto`; this capability adds storage and lifecycle
> around them, no cryptography of its own.

### Requirement: An entity identity survives the process
The system SHALL be able to store a local entity's identity in a keyfile and reload it on a
later run, so that a public key published to another node stays valid across restarts. The
keyfile SHALL record at minimum the 32-byte seed, the derived public key, the entity name and
the node type, and SHALL be readable by a later version through a documented format.

#### Scenario: Keyfile absent
- **WHEN** an entity is requested with a keyfile path that does not exist and generation is requested
- **THEN** a new keypair is generated, written to that path, and the same identity is loaded

#### Scenario: Keyfile present
- **WHEN** an entity is loaded from an existing keyfile
- **THEN** the public key and node hash are identical to those of the run that created it

#### Scenario: Public key stored in the file matches the seed
- **WHEN** a keyfile is loaded
- **THEN** the public key derived from the stored seed is compared against the stored public key, and a mismatch is an error naming the file rather than a silently preferred value

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
