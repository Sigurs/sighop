## MODIFIED Requirements

### Requirement: A private key is never stored in the clear
The system SHALL encrypt an entity's private key before it reaches the database, using a secret
supplied by the environment and held nowhere in the database, and SHALL store no representation
from which the private key can be recovered without that secret. A stored key SHALL carry an
authentication tag, so tampering with the stored value is detected rather than yielding a
different key.

#### Scenario: Storing an identity
- **WHEN** an entity identity is written to the database
- **THEN** the stored key material is ciphertext, the plaintext private key appears in no column, and the row alone is insufficient to reconstruct the identity

#### Scenario: Stored key material is altered
- **WHEN** stored key material is modified and then loaded
- **THEN** loading fails with an authentication error naming the entity, and no key is produced

#### Scenario: Loaded identity matches the stored public key
- **WHEN** an entity is loaded and its private key decrypted
- **THEN** the public key derived from that private key is compared against the public key stored beside it, and a mismatch is an error naming the entity rather than a silently preferred value

### Requirement: Identities can be imported and exported deliberately
The system SHALL provide an explicit action to import an identity into the store — from a
keyfile, or from a private key the operator supplies directly — and an explicit action to export
a stored identity back to a keyfile, so an operator can bring an identity in and back it up on
purpose. Importing a supplied private key SHALL write no keyfile, so an operator moving an
identity into the store is not required to leave unencrypted key material on disk. Export SHALL
be a distinct command from inspection, SHALL state that the written file contains private key
material, and SHALL write it with owner-only permissions.

#### Scenario: Importing a keyfile
- **WHEN** an identity keyfile is imported
- **THEN** an entity row is created with the private key encrypted, the same public key and node hash, and the operation reports the public key it stored

#### Scenario: Importing a supplied private key
- **WHEN** a 64-byte private key is supplied to the import action directly
- **THEN** an entity row is created with that private key encrypted, the operation reports the public key it derived, and no keyfile is written at any point

#### Scenario: Importing an identity already stored
- **WHEN** a keyfile whose public key already exists in the store is imported
- **THEN** the import fails naming the existing entity, and the stored row is unchanged

#### Scenario: A supplied private key already stored
- **WHEN** a private key whose public key already exists in the store is supplied to the import action
- **THEN** the import fails naming the existing entity, and the stored row is unchanged

#### Scenario: Exporting an identity
- **WHEN** a stored identity is exported to a path
- **THEN** a keyfile readable by the owner only is written, the output states that it holds private key material, and the stored row is unchanged

#### Scenario: Export targets an existing file
- **WHEN** export is asked to write to a path that already exists
- **THEN** it fails naming the path, and the file's contents are unchanged

## ADDED Requirements

### Requirement: Key material stored under the removed format is refused by name
The system SHALL refuse an entity row whose sealed key material was written under the removed
format that sealed a 32-byte seed, and SHALL say that is what happened rather than reporting the
row as corrupt or as failing to authenticate — the operator needs to know the row is intact and
the format is gone, because those call for different actions. The system SHALL NOT convert such a
row, and SHALL offer no command that does.

#### Scenario: A row sealed under the removed format
- **WHEN** an entity row whose sealed value holds a 32-byte seed is loaded
- **THEN** it is refused naming the entity and stating that the seed format is no longer supported, the row is left unchanged, and no identity is produced

#### Scenario: The refusal is distinguishable from corruption
- **WHEN** a row under the removed format and a row whose ciphertext has been altered are each loaded
- **THEN** the two produce different messages, and neither is reported as the other

#### Scenario: A run holding both kinds of row
- **WHEN** a run loads identities and one stored row is under the removed format
- **THEN** that row is refused by name and the run reports it, and the identities that do open are still loaded

#### Scenario: Newly stored key material
- **WHEN** an identity is stored after this change
- **THEN** the sealed value holds the 64-byte private key, and opening it needs the same secret as before

### Requirement: A stored identity can be removed deliberately
The system SHALL provide an explicit action to remove a stored identity, so that an identity
under the removed seed format can be replaced by the same identity supplied as a private key —
without which the stranded row blocks its own recovery, because the public key it holds is
already taken. Removal SHALL be irreversible and SHALL say so before it happens.

The system SHALL refuse to remove an identity that a room or a bot is bound to, naming what it
serves, because those rows are deleted with it and a room's history is not something an operator
can be assumed to have meant to discard. The system SHALL require confirmation that names the
identity, or an explicit flag accepting the loss where no terminal is available, and SHALL remove
nothing when confirmation is absent or does not match.

#### Scenario: Removing an identity
- **WHEN** a stored identity that nothing is bound to is removed with confirmation
- **THEN** the row is deleted, the removal is reported, and the identity no longer appears in the listing

#### Scenario: Removing an identity a room or bot is bound to
- **WHEN** removal is asked for an identity that a room or a bot is bound to
- **THEN** it fails naming what the identity serves, and the row is unchanged

#### Scenario: Removal without confirmation
- **WHEN** removal is asked for without confirmation and without the flag that accepts the loss
- **THEN** nothing is removed and the output states what the removal would cost

#### Scenario: Confirmation that does not match
- **WHEN** removal is confirmed with text that is not the identity's name
- **THEN** nothing is removed and the output says it was not confirmed

#### Scenario: Removing a row under the removed format
- **WHEN** removal is asked for an identity whose stored key material is a seed
- **THEN** it succeeds, and the output states that the row's key material could not be read anyway so nothing usable was lost

#### Scenario: Re-importing after removal
- **WHEN** an identity is removed and the same private key is then imported
- **THEN** the import succeeds and the stored identity has the public key and node hash it had before

### Requirement: The schema migration states what will stop opening and reads no key material
The system SHALL apply the schema change for this change without reading, decrypting or
rewriting any key material, so that it runs with no encryption secret available, and SHALL report
how many stored entity rows are under the removed format and will therefore stop opening. The
count SHALL be a count only: no name, no public key and no ciphertext.

#### Scenario: Migrating with rows under the removed format present
- **WHEN** the schema migration is applied against a database holding rows under the removed format
- **THEN** it succeeds, reports how many rows will stop opening, and alters no row's key material

#### Scenario: Migrating without the encryption secret
- **WHEN** the schema migration is applied with no encryption secret available
- **THEN** it succeeds, because it reads and rewrites no key material

#### Scenario: The report discloses nothing
- **WHEN** the migration reports affected rows
- **THEN** the output carries a count and no entity name, public key or stored ciphertext
