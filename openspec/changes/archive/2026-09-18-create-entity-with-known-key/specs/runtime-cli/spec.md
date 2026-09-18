## MODIFIED Requirements

### Requirement: Identity management commands cover the entity store
The system SHALL extend its key management command surface with actions to list stored
identities, import an identity into the store, and export a stored identity to a keyfile, and
SHALL generate an encryption secret on request. The import action SHALL accept either a keyfile
or a private key supplied on the command line, and SHALL require exactly one of the two. None of
these SHALL print a private key except the export, which writes it to a file rather than to the
terminal.

#### Scenario: Listing stored identities
- **WHEN** the list command is run against a configured database
- **THEN** each stored entity's name, type, public key, node hash and enabled state is printed, and no private key or ciphertext appears

#### Scenario: Importing a keyfile
- **WHEN** the import command is run with a keyfile
- **THEN** the identity is stored with its private key encrypted and the stored public key is printed

#### Scenario: Importing a supplied private key
- **WHEN** the import command is run with a private key and a name instead of a keyfile
- **THEN** the identity is stored with that private key encrypted, the derived public key and node hash are printed, and no file is written

#### Scenario: Import given both a keyfile and a private key
- **WHEN** the import command is run with both a keyfile and a private key
- **THEN** it fails saying the two are alternatives, and nothing is stored

#### Scenario: Import given neither
- **WHEN** the import command is run with neither a keyfile nor a private key
- **THEN** it fails saying one of the two is required, and nothing is stored

#### Scenario: A supplied private key the system will not accept
- **WHEN** the import command is run with a private key of the wrong length, one that is not hexadecimal, one that is not clamped, or one deriving a reserved or colliding node hash
- **THEN** it fails naming which of those it is, and no row is written

#### Scenario: Generating an encryption secret
- **WHEN** the secret generation command is run
- **THEN** a correctly-formed secret is printed once, with the statement that losing it makes every stored identity unrecoverable

#### Scenario: A store command run with no database configured
- **WHEN** a command that requires the entity store is run with no database configured
- **THEN** it fails saying a database is required for that action, and the keyfile commands remain usable

### Requirement: A key management command creates and inspects identities
The system SHALL provide a command that creates an entity keyfile and prints its public key, and
a command that prints an existing keyfile's name, node type, public key and node hash without
printing its private key. The creation command SHALL accept an optional private key supplied by
the operator and write a keyfile for that identity instead of generating one, and SHALL offer no
way to supply a seed.

#### Scenario: Creating an identity
- **WHEN** the key creation command is run with a name and an output path
- **THEN** a keyfile is written and the public key is printed in hex, suitable for entry into another node's contact list

#### Scenario: Creating an identity from a supplied private key
- **WHEN** the key creation command is run with a name, an output path and a private key
- **THEN** a keyfile holding that private key is written, the public key printed is the one that key derives, and no key is generated

#### Scenario: Creating from a private key the system will not accept
- **WHEN** the key creation command is run with a private key of the wrong length, one that is not hexadecimal, one that is not clamped, or one deriving a reserved node hash
- **THEN** it fails naming which of those it is, and no file is written

#### Scenario: Creating from a supplied private key over an existing file
- **WHEN** the key creation command is run with a private key and an output path that already exists
- **THEN** it fails naming the path, and the file's contents are unchanged

#### Scenario: Inspecting an identity
- **WHEN** the key inspection command is run against a keyfile
- **THEN** the name, node type, public key and node hash are printed, and the private key is not

## ADDED Requirements

### Requirement: A key management command removes a stored identity
The system SHALL provide a command that removes one stored identity, selected the same way the
export command selects one, so that an identity stranded by a format change can be replaced by
the same identity supplied as a private key. The command SHALL state what the removal costs
before it happens, SHALL confirm by asking for the identity's name at a terminal, and SHALL
accept an explicit flag in place of that confirmation where no terminal is available. It SHALL
refuse an identity a room or a bot is bound to, and SHALL remove nothing whenever it refuses.

#### Scenario: Removing a stored identity
- **WHEN** the removal command is run for an identity nothing is bound to and the confirmation matches its name
- **THEN** the row is removed and the output names the identity and its public key

#### Scenario: Removal with no terminal and no flag
- **WHEN** the removal command is run without a terminal and without the flag that accepts the loss
- **THEN** it fails saying what the removal would cost and how to accept it, and nothing is removed

#### Scenario: Removal naming a reference that matches several identities
- **WHEN** the removal command is run with a reference matching more than one stored identity
- **THEN** it fails listing what matched, and nothing is removed

#### Scenario: Removal of an identity in use
- **WHEN** the removal command is run for an identity a room or a bot is bound to
- **THEN** it fails naming what it serves, and nothing is removed
