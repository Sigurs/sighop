# Spec Delta

## MODIFIED Requirements

### Requirement: A keyfile is an interchange format, not the store of record
Where a database is configured, the system SHALL treat the entity store as the store of record
for a local identity and a keyfile as a means of moving one in or out. The system SHALL NOT
write an identity's private key to a keyfile as a side effect of ordinary operation, and SHALL
require a distinct, explicit action to produce one.

#### Scenario: Ordinary run with persisted entities
- **WHEN** the runtime runs with a database configured and entities in the entity store
- **THEN** no keyfile is written and no private key reaches the filesystem

#### Scenario: Creating an identity with a database configured
- **WHEN** an identity is created with a database configured and no keyfile path requested
- **THEN** the identity is written to the entity store with its private key encrypted, and no keyfile is produced

#### Scenario: Producing a keyfile from a stored identity
- **WHEN** an export of a stored identity is explicitly requested
- **THEN** a keyfile is written with owner-only permissions and the operation states that the file holds private key material
