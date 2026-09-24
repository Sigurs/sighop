# Spec Delta

## MODIFIED Requirements

### Requirement: Rooms are configured through the interface
The system SHALL allow creating a room on an identity, listing rooms with their member and message
counts, setting and rotating the administrator and guest passwords, setting guest access and
read-only fallback, setting or clearing the two retention bounds, and setting or clearing the
room's push acknowledgement window and its delivery recency limit, and SHALL state the consequence
of a password rotation and of a member revocation before either is applied.

#### Scenario: Rotating a password
- **WHEN** a room password is rotated
- **THEN** the interface states, before applying it, that existing members must log in again with the new password

#### Scenario: Setting retention
- **WHEN** a retention bound is set
- **THEN** the interface states how many stored messages the bound would remove before it is applied

#### Scenario: A password is never placed in a URL
- **WHEN** a password is submitted
- **THEN** it is not carried in a query string, not reflected in any served page, and not recorded in the request event

#### Scenario: Setting delivery settings
- **WHEN** the delivery page of a room is opened
- **THEN** it shows the room's push acknowledgement window and recency limit, states the firmware windows that apply while the window is blank, and states that a member skipped for recency keeps its position and resumes when heard

#### Scenario: Clearing delivery settings
- **WHEN** either delivery field is submitted blank
- **THEN** that setting is cleared, restoring the firmware window or delivery to every member respectively

#### Scenario: An invalid delivery setting
- **WHEN** a delivery field is submitted outside its allowed range or not as a whole number
- **THEN** the page is shown again with the reason, and neither setting is changed

### Requirement: Identity, room and bot writes reach the running process without a restart

The system SHALL apply a write made through the interface to the running process that served the
page, without a restart and without waiting for any periodic re-read, for every write that changes
what that process holds: creating, importing, enabling, disabling and removing an identity,
changing an identity's advert configuration, creating a room, changing a room's access, retention
or delivery settings, and creating, enabling and disabling a bot. This is the behaviour the
interface already gives for adding a channel and for renaming an identity, and it SHALL be the
behaviour of these surfaces too. A room setting changed by another process SHALL reach a running
process on its next periodic re-read.

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

#### Scenario: Changing a served room's settings

- **WHEN** a served room's access, retention or delivery settings are changed through the interface
- **THEN** the running process applies them to that room without a restart — logins are checked against the new access settings, the next retention pass uses the new bounds, and pushes composed afterwards use the new delivery settings

#### Scenario: Creating a bot

- **WHEN** a bot is created through the interface on an enabled entity this run holds
- **THEN** the running process runs it without a restart, and the result states that the bot is now running

#### Scenario: Stored but not taken up

- **WHEN** a write is stored and the running process does not take it up, because the identity is not enabled or its node hash collides with one this run holds
- **THEN** the result states that the store took the write, that this run did not take it up, and the reason

#### Scenario: The identities page reflects the change at once

- **WHEN** the identities page is reloaded after any of these writes
- **THEN** it shows the identities this run now holds, with their advert schedules, matching what the run is actually doing
