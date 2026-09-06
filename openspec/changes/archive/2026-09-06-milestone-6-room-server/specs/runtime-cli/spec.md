## ADDED Requirements

> Reference: DESIGN.md §7 (room administration, password policy, *"the room member list therefore
> needs a revoke action, or there is no way to remove anyone"*), §6 (retention is policy-driven per
> room), §12 (a run states what it is doing before it does it).

### Requirement: A room command surface manages rooms, passwords and members
The system SHALL provide commands to create a room on a stored identity, to show a room's
configuration, to list rooms, to change a room's admin or guest password, to list and revoke
members, to set or clear a retention policy, to post to a room as the server itself, and to read a
room's stored history.

#### Scenario: Creating a room
- **WHEN** a room is created on a stored room-server identity
- **THEN** the room exists with the given name, its admin password stored as a hash, guest access refused unless configured, and retention unlimited, and the output states each of those

#### Scenario: Showing a room
- **WHEN** a room is shown
- **THEN** the output names its identity, its membership count, its message count, its guest-access setting and its retention policy, and never any password or hash

#### Scenario: Listing members
- **WHEN** a room's members are listed
- **THEN** each member's public key, permission level, sync position and last activity are reported

#### Scenario: Reading history
- **WHEN** a room's history is read
- **THEN** the stored messages are reported with author, ordering value and text, with text that is not valid UTF-8 marked as a rendering rather than presented as the author's text

#### Scenario: Posting as the server
- **WHEN** an operator posts to a room
- **THEN** the post is stored authored by the room's own identity and becomes deliverable to every member

### Requirement: A password is never accepted as a command-line argument
The system SHALL read every room password from an interactive prompt or from standard input, and
SHALL NOT accept one as a command-line argument, because process arguments are readable by other
users on the host.

#### Scenario: A password is required
- **WHEN** a command that needs a password is run without one available on standard input
- **THEN** it prompts for the password without echoing it, rather than reading one from its arguments

#### Scenario: A password is supplied on the command line
- **WHEN** a password is passed as a command-line argument
- **THEN** the command refuses and says why, rather than accepting it

### Requirement: Rotating a password and revoking a member each state their consequence
The system SHALL state, when a password is rotated, that existing members keep their access and
only new logins are gated, and SHALL state, when a member is revoked, that the member must log in
again and that its sync position and replay guard are discarded with it.

#### Scenario: Rotating the guest password
- **WHEN** a room's guest password is changed
- **THEN** the output says that existing members are unaffected and that removing a member requires revoking it

#### Scenario: Revoking a member
- **WHEN** a member is revoked
- **THEN** the output names the member and states that it must log in again and that its sync position is gone

### Requirement: A run serves the rooms that are configured and says what it is serving
The system SHALL serve every room bound to an enabled entity when it starts, SHALL report each of
them before any traffic is handled, and SHALL state plainly when no database is configured that no
rooms exist.

#### Scenario: A run with rooms configured
- **WHEN** the runtime starts with rooms in the database
- **THEN** each room, its identity, membership count, message count and retention policy are reported before the first frame is handled

#### Scenario: A run without a database
- **WHEN** the runtime starts with no database configured
- **THEN** the output states that no rooms are served because rooms require durable storage, rather than omitting the subject

#### Scenario: A room whose entity is disabled
- **WHEN** a room is bound to an entity that is not enabled
- **THEN** it is not served and is reported as not served, with the reason
