## ADDED Requirements

### Requirement: Exporting an identity is a guarded action
The system SHALL treat writing a stored identity's key material out of the platform as a
guarded action of the same kind as revealing it: confirmed explicitly for that specific
identity, produced in exactly one response, not reachable by following a link, and recorded
as its own structured event naming the identity. The exported material SHALL be the same
unencrypted seed the command line writes, so that a file produced here and a file produced
by the command line are interchangeable. The interface SHALL state how the two differ in
protection: the command line writes the file with owner-only permissions in the same call
that creates it, and a file delivered to a browser has whatever protection the browser's
download location gives it, which is usually none.

#### Scenario: Exporting an identity
- **WHEN** an operator asks to export a stored identity
- **THEN** the action is confirmed explicitly, the file is produced in that one response, and the export is recorded as its own event naming the identity

#### Scenario: An export that was not confirmed
- **WHEN** an export is requested without the confirmation that view issued
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
applying migrations and generating the secret that seals stored identities.

#### Scenario: Looking for a migration control
- **WHEN** an operator looks at the schema information
- **THEN** the interface states that migrations are applied deliberately from a terminal and are not offered here, and why

#### Scenario: Looking for secret generation
- **WHEN** an operator looks at identity administration
- **THEN** the interface states that the sealing secret is generated from a terminal and not here, and why
