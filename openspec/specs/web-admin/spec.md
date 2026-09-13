# web-admin Specification

## Purpose

Configuration through the browser: the identities the platform runs, the rooms and bots bound to
them, their passwords, retention and driver settings, and the radio the whole thing sits on. It is
also where the platform's most dangerous actions live — revealing a private key, opening the
transmit gate, raising the airtime ceiling — so it is where those actions are made deliberate and
recorded.

## Requirements

### Requirement: Configuration writes go through the same rules the command line uses
The system SHALL apply every configuration change made through the interface using the same
validation, uniqueness and refusal rules that the command line applies for the same change, so that
no rule exists in one surface and not the other.

#### Scenario: A refusal the command line would make
- **WHEN** a change is submitted that the command line would refuse
- **THEN** the interface refuses it for the same reason and states that reason

#### Scenario: A change made in either surface
- **WHEN** a change is made through the interface
- **THEN** its stored result is indistinguishable from the same change made through the command line

### Requirement: Identities are listed, created and managed
The system SHALL list the stored identities with their name, type, public key, node hash, enabled
state and creation time, SHALL allow creating a new identity, enabling and disabling one, and
importing and exporting one, and SHALL state what an exported keyfile is: an unencrypted seed
protected only by its file permissions.

#### Scenario: Listing identities
- **WHEN** the identities page is opened
- **THEN** every stored identity is listed with its public key and node hash in full

#### Scenario: Exporting an identity
- **WHEN** an identity is exported
- **THEN** the interface states that the exported material is an unencrypted seed and that it offers protection different from the store's

#### Scenario: Disabling an identity in use
- **WHEN** an identity bound to a running room or bot is disabled
- **THEN** the interface states what that identity is serving before the change is applied

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

### Requirement: Rooms are configured through the interface
The system SHALL allow creating a room on an identity, listing rooms with their member and message
counts, setting and rotating the administrator and guest passwords, setting guest access and
read-only fallback, and setting or clearing the two retention bounds, and SHALL state the
consequence of a password rotation and of a member revocation before either is applied.

#### Scenario: Rotating a password
- **WHEN** a room password is rotated
- **THEN** the interface states, before applying it, that existing members must log in again with the new password

#### Scenario: Setting retention
- **WHEN** a retention bound is set
- **THEN** the interface states how many stored messages the bound would remove before it is applied

#### Scenario: A password is never placed in a URL
- **WHEN** a password is submitted
- **THEN** it is not carried in a query string, not reflected in any served page, and not recorded in the request event

### Requirement: Bots are configured through the interface
The system SHALL list the configured bots with their driver, bound identity, enabled state and
mode, SHALL allow creating one, enabling and disabling it, switching between observing and active,
and editing its driver configuration, and SHALL state that switching a bot to active means it will
transmit without further prompting.

#### Scenario: Switching a bot to active
- **WHEN** a bot's mode is changed to active
- **THEN** the interface states that the bot will transmit unprompted, and the change is confirmed explicitly

#### Scenario: Invalid driver configuration
- **WHEN** a driver configuration is submitted that the driver rejects
- **THEN** the change is refused with the driver's own reason and the stored configuration is unchanged

#### Scenario: A bot's durable state is visible
- **WHEN** a bot's page is opened
- **THEN** the state the bot has persisted is readable, including the records that decide whether it will act on a given contact again

### Requirement: Radio parameters are shown as the board reports them, and a change states its scope
The system SHALL display the radio parameters in force as the board's own readback reports them,
and SHALL, where it offers to change them, state that the change is not persisted by the board and
is lost on a board reset.

#### Scenario: Displaying the radio
- **WHEN** the radio page is opened
- **THEN** the parameters shown are the board's readback, and any value the board did not answer is shown as absent

#### Scenario: Changing a parameter
- **WHEN** a radio parameter is changed
- **THEN** the interface states that the board does not persist it and that a reset reverts to the board's build defaults

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

### Requirement: A bot's greeting records can be changed, and each change states what it releases
The system SHALL allow an operator to change the records that decide whether a bot will act
on a given contact again: clearing one, setting one, and seeding them for every contact
already known. It SHALL state, before a record is cleared, that clearing it makes that
contact eligible to be acted on again — including transmitted at, when the bot is active.

#### Scenario: Clearing a record
- **WHEN** a greeting record is cleared for a contact
- **THEN** the interface states beforehand that the bot may act on that contact again, and afterwards the record is gone

#### Scenario: Recording a contact as already acted on
- **WHEN** a contact is recorded as already greeted
- **THEN** the bot will not act on that contact, and the record shows that an operator set it

#### Scenario: Seeding an existing store
- **WHEN** records are seeded for every contact already known
- **THEN** contacts that already had a record keep the one they had

### Requirement: A bot's durable state can be cleared, and what that forgets is stated
The system SHALL allow an operator to clear everything a bot has persisted, and SHALL state
before doing so what that forgets — for a greeter, every record of who has been greeted,
which makes every known contact eligible again.

#### Scenario: Clearing a bot's state
- **WHEN** a bot's durable state is cleared
- **THEN** the interface states what is forgotten before the change is applied, and reports how many keys were removed

### Requirement: The schema revision this run is against is visible
The system SHALL show the revision the database reports and the revision this code expects,
and SHALL say whether they agree. It SHALL NOT offer to apply migrations.

#### Scenario: A database at the expected revision
- **WHEN** the schema page is opened against a database at the revision the code expects
- **THEN** both revisions are shown and the interface says they agree

#### Scenario: A database at a different revision
- **WHEN** the revisions differ
- **THEN** both are named, the disagreement is stated, and the command that reconciles them is given

#### Scenario: Migrations are not applied from the interface
- **WHEN** the schema page is used
- **THEN** it offers no action that applies a migration

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
