## MODIFIED Requirements

### Requirement: Revealing key material, enabling transmit, and raising the ceiling are re-confirmed and audited
The system SHALL treat revealing a private key, enabling transmission, and raising the airtime
ceiling above the regulatory default as distinct guarded actions. Each SHALL require an explicit
confirmation of that specific action carrying the signed-in user's password, SHALL NOT be
reachable by following a link or by a request that could be issued incidentally, and SHALL emit
its own structured event naming the action, its target, its outcome and the acting user.

#### Scenario: Revealing a private key
- **WHEN** an operator asks to see an identity's private key material
- **THEN** the action is confirmed explicitly with the operator's password, the reveal is recorded as its own event naming the identity and the operator, and the material is not present in any page served before that confirmation

#### Scenario: Enabling transmission
- **WHEN** transmission is enabled through the interface
- **THEN** the action is confirmed explicitly with the operator's password and recorded as its own event naming the operator, and the panel's gate indication changes to match

#### Scenario: Raising the ceiling
- **WHEN** the airtime ceiling is raised above the regulatory default
- **THEN** the action is confirmed explicitly with the operator's password, the confirmation states that the default is a regulatory limit, and the action is recorded as its own event carrying the old and new values and the operator

#### Scenario: A guarded action is never a bare link
- **WHEN** the interface is navigated
- **THEN** no guarded action is performed by a request that a browser could issue by following, prefetching or reloading a page

### Requirement: Exporting an identity is a guarded action
The system SHALL treat writing a stored identity's key material out of the platform as a
guarded action of the same kind as revealing it: confirmed explicitly for that specific
identity with the signed-in user's password, produced in exactly one response, not reachable by
following a link, and recorded as its own structured event naming the identity and the acting
user. The exported material SHALL be the same unencrypted seed the command line writes, so that
a file produced here and a file produced by the command line are interchangeable. The interface
SHALL state how the two differ in protection: the command line writes the file with owner-only
permissions in the same call that creates it, and a file delivered to a browser has whatever
protection the browser's download location gives it, which is usually none.

#### Scenario: Exporting an identity
- **WHEN** an operator asks to export a stored identity
- **THEN** the action is confirmed explicitly with the operator's password, the file is produced in that one response, and the export is recorded as its own event naming the identity and the operator

#### Scenario: An export that was not confirmed
- **WHEN** an export is requested without the confirmation that view issued, or without the operator's correct password
- **THEN** nothing is produced, and the refusal is recorded as its own event

#### Scenario: The file is the command line's file
- **WHEN** an identity is exported through the interface and through the command line
- **THEN** the two files carry the same identity and are usable interchangeably

#### Scenario: The difference in protection is stated
- **WHEN** an export is offered
- **THEN** the interface states that the command line's file is created owner-only and a downloaded one is not

### Requirement: Capabilities this build deliberately does not offer are named where they would be looked for
The system SHALL state, in the interface itself, which command-line capabilities it does not
expose and why, rather than leaving their absence to be discovered. This covers at minimum
applying migrations, generating the secret that seals stored identities, and managing the
accounts that sign in to the interface.

#### Scenario: Looking for a migration control
- **WHEN** an operator looks at the schema information
- **THEN** the interface states that migrations are applied deliberately from a terminal and are not offered here, and why

#### Scenario: Looking for secret generation
- **WHEN** an operator looks at identity administration
- **THEN** the interface states that the sealing secret is generated from a terminal and not here, and why

#### Scenario: Looking for account management
- **WHEN** a signed-in operator looks for a way to add an account, change a password or disable an account
- **THEN** the interface names the terminal command that does it and states why it is not offered in the browser
