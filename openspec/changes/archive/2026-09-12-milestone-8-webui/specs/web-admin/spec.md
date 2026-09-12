## Purpose

Configuration through the browser: the identities the platform runs, the rooms and bots bound to
them, their passwords, retention and driver settings, and the radio the whole thing sits on. It is
also where the platform's most dangerous actions live — revealing a private key, opening the
transmit gate, raising the airtime ceiling — so it is where those actions are made deliberate and
recorded.

## ADDED Requirements

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
confirmation of that specific action, SHALL NOT be reachable by following a link or by a request
that could be issued incidentally, and SHALL emit its own structured event naming the action, its
target and its outcome.

#### Scenario: Revealing a private key
- **WHEN** an operator asks to see an identity's private key material
- **THEN** the action is confirmed explicitly, the reveal is recorded as its own event naming the identity, and the material is not present in any page served before that confirmation

#### Scenario: Enabling transmission
- **WHEN** transmission is enabled through the interface
- **THEN** the action is confirmed explicitly and recorded as its own event, and the panel's gate indication changes to match

#### Scenario: Raising the ceiling
- **WHEN** the airtime ceiling is raised above the regulatory default
- **THEN** the action is confirmed explicitly, the confirmation states that the default is a regulatory limit, and the action is recorded as its own event carrying the old and new values

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
