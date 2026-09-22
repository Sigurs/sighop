# Spec Delta

## ADDED Requirements

### Requirement: Identity, room and bot writes reach the running process without a restart

The system SHALL apply a write made through the interface to the running process that served the
page, without a restart and without waiting for any periodic re-read, for every write that changes
what that process holds: creating, importing, enabling, disabling and removing an identity,
changing an identity's advert configuration, creating a room, and creating, enabling and disabling
a bot. This is the behaviour the interface already gives for adding a channel and for renaming an
identity, and it SHALL be the behaviour of these surfaces too.

The interface SHALL state the effect on the running process in the result of each such write: that
the identity is now held by this run and advertising, that it is no longer held, that the room is
now served, or that the bot is now running — and, where the write did not reach this run, why.

Where the write is stored but the running process does not take it up, the system SHALL state that
distinction plainly rather than reporting success: the store took the write and this run did not,
and an operator must be able to tell those apart without reading the event stream.

#### Scenario: Creating an identity

- **WHEN** an identity is created through the interface
- **THEN** the running process holds it and adverts for it without a restart, and the result states that it is now held by this run

#### Scenario: Disabling an identity

- **WHEN** a loaded identity is disabled through the interface
- **THEN** the running process stops advertising for it without a restart, and the result states that this run no longer holds it

#### Scenario: Removing an identity

- **WHEN** a stored identity is removed through the interface under the rules the command line applies
- **THEN** the running process withdraws it without a restart, and the result states that this run no longer holds it

#### Scenario: Creating a room

- **WHEN** a room is created through the interface on an identity this run holds
- **THEN** the running process serves it without a restart, and the result states that the room is now served

#### Scenario: Creating a bot

- **WHEN** a bot is created through the interface on an enabled entity this run holds
- **THEN** the running process runs it without a restart, and the result states that the bot is now running

#### Scenario: Stored but not taken up

- **WHEN** a write is stored and the running process does not take it up, because the identity is not enabled or its node hash collides with one this run holds
- **THEN** the result states that the store took the write, that this run did not take it up, and the reason

#### Scenario: The identities page reflects the change at once

- **WHEN** the identities page is reloaded after any of these writes
- **THEN** it shows the identities this run now holds, with their advert schedules, matching what the run is actually doing

### Requirement: A disabling that stops a room or a bot states that before it is applied

The system SHALL state, before disabling or removing an identity that a served room or a running
bot is bound to, that the running process will stop serving that room or running that bot, naming
it. The interface already states what such an identity is serving; it SHALL also state that the
consequence is immediate and does not wait for a restart, so that an operator is not told about a
binding while being left to assume the effect is deferred.

The system SHALL state that a room's membership and stored messages, and a bot's durable state,
survive the disabling and return if the identity is enabled again.

#### Scenario: Disabling an identity that serves a room

- **WHEN** disabling is offered for an identity a served room is bound to
- **THEN** the interface names the room, states that this run will stop serving it immediately, and states that the room's members and messages survive

#### Scenario: Disabling an identity that runs a bot

- **WHEN** disabling is offered for an identity a running bot is bound to
- **THEN** the interface names the bot, states that this run will stop running it immediately, and states that the bot's durable state survives

#### Scenario: Disabling an identity held as a default

- **WHEN** disabling is offered for an identity an operator holds as their default chat identity
- **THEN** the interface states that this run will stop holding it and that no identity will be preselected in that operator's composers
