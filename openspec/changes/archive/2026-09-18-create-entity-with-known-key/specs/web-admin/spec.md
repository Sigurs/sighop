## MODIFIED Requirements

### Requirement: Capabilities this build deliberately does not offer are named where they would be looked for
The system SHALL state, in the interface itself, which command-line capabilities it does not
expose and why, rather than leaving their absence to be discovered. This covers at minimum
applying migrations, generating the secret that seals stored identities, managing the
accounts that sign in to the interface, revealing a stored channel pre-shared key, and removing
a stored identity.

#### Scenario: Looking for a migration control
- **WHEN** an operator looks at the schema information
- **THEN** the interface states that migrations are applied deliberately from a terminal and are not offered here, and why

#### Scenario: Looking for secret generation
- **WHEN** an operator looks at identity administration
- **THEN** the interface states that the sealing secret is generated from a terminal and not here, and why

#### Scenario: Looking for account management
- **WHEN** a signed-in operator looks for a way to add an account, change a password or disable an account
- **THEN** the interface names the terminal command that does it and states why it is not offered in the browser

#### Scenario: Looking for a channel's pre-shared key
- **WHEN** an operator looks at a pre-shared-key channel to share its key with someone
- **THEN** the interface names the terminal command that prints it and states why it is not shown in the browser

#### Scenario: Looking for a way to remove an identity
- **WHEN** an operator looks at identity administration for a way to remove a stored identity
- **THEN** the interface names the terminal command that removes one, states that disabling is the reversible action offered here, and says that removal is irreversible

### Requirement: Identities are listed, created and managed
The system SHALL list the stored identities with their name, type, public key, node hash, enabled
state and creation time, SHALL allow creating a new identity, enabling and disabling one, and
importing and exporting one, and SHALL state what an exported keyfile is: an unencrypted private
key protected only by its file permissions. Creating an identity SHALL accept an optional private
key supplied by the operator and use it in place of generating one, and SHALL refuse a supplied
key for the same reasons and in the same words as the command line does, re-rendering the page
with the reason and creating nothing.

#### Scenario: Listing identities
- **WHEN** the identities page is opened
- **THEN** every stored identity is listed with its public key and node hash in full

#### Scenario: Creating an identity from a supplied private key
- **WHEN** an identity is created with a private key supplied in the form
- **THEN** the stored identity has the public key and node hash that key derives, no key is generated, and the identity is indistinguishable from one the command line created from the same key

#### Scenario: Creating an identity with the key field left empty
- **WHEN** an identity is created with the private key field left empty
- **THEN** a key is generated as before

#### Scenario: A supplied private key the system will not accept
- **WHEN** an identity is created with a private key of the wrong length, one that is not hexadecimal, one that is not clamped, or one deriving a reserved or colliding node hash
- **THEN** the page is re-rendered with the same reason the command line gives, the other fields the operator typed are preserved, and no identity is stored

#### Scenario: Exporting an identity
- **WHEN** an identity is exported
- **THEN** the interface states that the exported material is an unencrypted private key and that it offers protection different from the store's

#### Scenario: Disabling an identity in use
- **WHEN** an identity bound to a running room or bot is disabled
- **THEN** the interface states what that identity is serving before the change is applied
