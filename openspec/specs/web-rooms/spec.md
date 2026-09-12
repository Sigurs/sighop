# web-rooms Specification

## Purpose

Reading a room the platform serves: the messages it has stored, in the order the protocol orders
them, and the members who are entitled to receive them, with each member's sync position — the one
view that answers "why has this person not seen that message" without a database client.

## Requirements

### Requirement: A room's stored history is browsable
The system SHALL present the messages a room has stored, newest first, in pages bounded in size,
each message carrying its ordering timestamp, its author, the time it was stored, and its text
rendered from the stored bytes with any byte sequence that is not displayable shown as such rather
than substituted silently.

#### Scenario: Reading a room's history
- **WHEN** a room's history page is opened
- **THEN** its stored messages are listed newest first with their ordering timestamps

#### Scenario: Paging back
- **WHEN** more messages exist than one page holds
- **THEN** older messages are reachable, and the ordering across pages is the same total order the protocol uses

#### Scenario: A message whose bytes are not displayable text
- **WHEN** a stored message's bytes are not valid displayable text
- **THEN** the message is shown with that fact stated and its bytes available, rather than silently replaced

### Requirement: An author is identified by key, and by name only where a name is authenticated
The system SHALL identify each message's author by the public key stored with it, and SHALL show a
name for that author only where the name comes from a source the platform verifies, marking any
other name as unauthenticated.

#### Scenario: An author who is a known contact
- **WHEN** a message's author key matches a contact whose advert was verified
- **THEN** the contact's name is shown alongside the key, marked as verified

#### Scenario: An author who is not a known contact
- **WHEN** a message's author key matches no verified contact
- **THEN** the key is shown without a name, rather than a name taken from an unverified source

### Requirement: A room's members are listed with their permissions and sync position
The system SHALL list a room's members with their public key, node hash, permission level, sync
cursor, last activity and first login, and SHALL show how many stored messages remain unsynced for
each member.

#### Scenario: Listing members
- **WHEN** a room's members page is opened
- **THEN** every member is listed with permission, sync cursor and unsynced count

#### Scenario: Two members sharing a node hash
- **WHEN** two members of a room share a node hash
- **THEN** both are listed and distinguished by their public keys, and the shared hash is not treated as a conflict

#### Scenario: Revoking a member
- **WHEN** a member is revoked from this view
- **THEN** the interface states that revocation removes membership, permissions, sync cursor and replay guard together, and the action is confirmed before it is applied

### Requirement: Browsing a room never transmits and never advances a cursor
The system SHALL treat every room view as read-only with respect to the mesh: opening, paging or
refreshing a view SHALL NOT transmit, SHALL NOT push history to any member, and SHALL NOT alter any
member's sync position.

#### Scenario: Opening a busy room's history
- **WHEN** a room's history is browsed repeatedly
- **THEN** nothing is transmitted and no member's cursor changes

### Requirement: Rooms the platform does not serve are shown as unserved
The system SHALL list every room the database holds, and SHALL distinguish the rooms this run is
serving from those it is not, giving the reason a room is unserved.

#### Scenario: A room whose identity is not loaded
- **WHEN** a room exists whose identity this run did not load
- **THEN** the room is listed as unserved with that reason, and its stored history is still browsable
