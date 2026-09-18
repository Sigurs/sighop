## RENAMED Requirements

- FROM: `### Requirement: A keyfile holds a seed in the clear and says so`
- TO: `### Requirement: A keyfile holds a private key in the clear and says so`

- FROM: `### Requirement: The seed is never printed by inspection commands`
- TO: `### Requirement: The private key is never printed by inspection commands`

## MODIFIED Requirements

### Requirement: An entity identity survives the process
The system SHALL be able to store a local entity's identity and reload it on a later run, so
that a public key published to another node stays valid across restarts. The system SHALL
support two stores for this: a keyfile, and — where a database is configured — the entity store,
which holds the same identity with its private key encrypted at rest. A keyfile SHALL record at
minimum the 64-byte private key in the representation the firmware stores, the derived public
key, the entity name and the node type, and SHALL be readable by a later version through a
documented format. Where both stores hold an identity for one run, the system SHALL state which
one each loaded entity came from rather than leaving the source to be inferred.

#### Scenario: Keyfile absent
- **WHEN** an entity is requested with a keyfile path that does not exist and generation is requested
- **THEN** a new keypair is generated, written to that path, and the same identity is loaded

#### Scenario: Keyfile present
- **WHEN** an entity is loaded from an existing keyfile
- **THEN** the public key and node hash are identical to those of the run that created it

#### Scenario: Public key stored in the file matches the seed
- **WHEN** a keyfile written under the removed format, which records a seed, is loaded
- **THEN** it is refused naming the file and the removed format, no seed is expanded and no identity is produced

#### Scenario: Public key stored in the file matches the private key
- **WHEN** a keyfile written under the current format, which records a private key, is loaded
- **THEN** the public key derived from the stored private key is compared against the stored public key, and a mismatch is an error naming the file rather than a silently preferred value

#### Scenario: Identity held in the entity store
- **WHEN** an entity is loaded from the entity store on a run with a database configured
- **THEN** the public key and node hash are identical to those it was stored with, and the startup output names the entity store as its source

#### Scenario: One run loads identities from both sources
- **WHEN** a run loads one identity from a keyfile and another from the entity store
- **THEN** both are usable local entities, each is reported with the source it came from, and the node-hash collision rule applies across both

### Requirement: A keyfile holds a private key in the clear and says so
The system SHALL state, wherever a keyfile is created or exported, that the file contains an
unencrypted private key and is protected only by its filesystem permissions — because the same
private key inside the entity store is encrypted, and an operator must not conclude that the two
offer the same protection.

#### Scenario: Creating a keyfile
- **WHEN** a keyfile is created
- **THEN** the output states that the file holds an unencrypted private key protected only by its permissions

#### Scenario: Exporting to a keyfile
- **WHEN** a stored identity is exported to a keyfile
- **THEN** the output states the same, naming the path written

### Requirement: The private key is never printed by inspection commands
The system SHALL provide a way to inspect an identity — name, public key, node type and node
hash — without disclosing the private key, and SHALL require a distinct, explicit action to
export private key material.

#### Scenario: Inspecting an identity
- **WHEN** an identity is inspected through the command-line surface
- **THEN** the output carries the name, public key, node type and node hash, and does not contain the private key

## ADDED Requirements

### Requirement: A keyfile declares its format version and only the current version is read
The system SHALL record a format version in every keyfile it writes, SHALL write the current
version only, and SHALL read the current version only. A keyfile recording a seed under the
removed earlier version SHALL be refused, naming the file and stating that the format recorded a
seed and is no longer supported — a refusal rather than a conversion, because a keyfile is
material the system does not own and rewriting one on the operator's behalf is not this system's
call to make. The system SHALL likewise refuse a keyfile declaring a version it does not know,
naming the version rather than guessing at the fields.

#### Scenario: Loading a keyfile written under the removed version
- **WHEN** a keyfile recording a 32-byte seed under the removed format version is loaded
- **THEN** it is refused naming the file and the version it found, the refusal states that the seed format is no longer supported, and no identity is produced

#### Scenario: The refusal says what can be done instead
- **WHEN** a keyfile under the removed version is refused
- **THEN** the message states that the identity has to be supplied as a private key or recreated, and names no conversion command, because there is none

#### Scenario: A keyfile is written in the current version only
- **WHEN** a keyfile is created or exported
- **THEN** it declares the current version and records the 64-byte private key, and records no seed

#### Scenario: Round trip through the current format
- **WHEN** an identity is exported to a keyfile and loaded back
- **THEN** the public key, node hash, name and node type are unchanged

#### Scenario: An unknown keyfile version
- **WHEN** a keyfile declaring a version the system does not know is loaded
- **THEN** it fails naming the file and the version it found, and no identity is produced

### Requirement: An identity can be created from a private key the operator already holds
The system SHALL allow an entity to be created from a 64-byte private key supplied by the
operator, in place of generating one, so that an identity held on another device or in a backup
can be brought into the system without being regenerated. The supplied key SHALL be the only
private input accepted; the system SHALL NOT offer a way to supply a seed. The public key, node
hash and every derived value SHALL be computed from the supplied key, and an identity so created
SHALL be indistinguishable in behaviour from a generated one.

#### Scenario: Creating from a supplied private key
- **WHEN** an entity is created with a 64-byte private key supplied by the operator
- **THEN** the entity's public key and node hash are the ones that key derives, and no key is generated

#### Scenario: The supplied key is the same identity it was elsewhere
- **WHEN** an identity is exported from the system and then re-created from the private key that export carries
- **THEN** the re-created identity has the same public key and node hash, and signs a message identically

### Requirement: A supplied private key is validated before anything is created
The system SHALL refuse a supplied private key that it cannot use, SHALL name the reason, and
SHALL create no entity, write no keyfile and store no row when it refuses. The system SHALL
refuse a key that is not exactly 64 bytes, a key whose scalar does not already carry the standard
clamping — because re-clamping it would silently produce a different identity from the one the
operator believes they supplied — a key deriving a public key whose node hash is one of the
reserved values the firmware rejects, and a key whose node hash collides with a local entity the
run already holds.

#### Scenario: A key of the wrong length
- **WHEN** a private key that is not 64 bytes is supplied
- **THEN** creation fails saying how many bytes were supplied and how many are expected, and nothing is created

#### Scenario: A key that is not valid hexadecimal
- **WHEN** a private key that is not valid hexadecimal is supplied
- **THEN** creation fails saying so, and nothing is created

#### Scenario: An unclamped key
- **WHEN** a 64-byte private key whose scalar is not already clamped is supplied
- **THEN** creation fails stating that the key is not in the expected representation and that the system will not alter it, and nothing is created

#### Scenario: A key deriving a reserved node hash
- **WHEN** a supplied key derives a public key beginning with a reserved node hash
- **THEN** creation fails stating that the firmware rejects that prefix, and nothing is created

#### Scenario: A key colliding with a local entity
- **WHEN** a supplied key derives a node hash already held by a local entity of this run
- **THEN** creation fails naming the collision, and nothing is created

#### Scenario: No public key is asked for alongside a supplied private key
- **WHEN** an identity is created from a supplied private key
- **THEN** the operator supplies the private key alone, and the public key is derived rather than accepted as an input that could disagree with it
