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

### Requirement: A room can be posted to as its own identity, and posting is guarded
The system SHALL allow an operator to post to a room the database holds, as that room's own
identity, storing the post exactly as a post arriving over the air is stored — with an
ordering timestamp in the room's own total order — so that it is delivered to every member
by the same push path any other post takes. A post reaches every member of a room and is
therefore not a configuration change: it SHALL be confirmed explicitly before it happens and
SHALL be recorded as its own structured event naming the room and the outcome.

#### Scenario: Posting to a room this run is serving
- **WHEN** an operator posts to a room this run serves
- **THEN** the post is confirmed first, is stored with an ordering timestamp in the room's total order, becomes deliverable to members after the reference implementation's hold, and is recorded as its own event

#### Scenario: Posting to a room this run does not serve
- **WHEN** a post is made to a room whose identity this run did not load
- **THEN** it is stored like any other post, and the interface states that nothing will be delivered until a run serving that room is started

#### Scenario: The transmit gate is closed
- **WHEN** a post is made while transmission is disabled
- **THEN** the interface states before the post is made that it will be stored now and put on the air only when the gate opens

#### Scenario: A post too long for a room to keep
- **WHEN** composed text exceeds what a room stores
- **THEN** it is refused with the limit and how far over it is, the author's text is preserved, and nothing is stored — because an author who is present can be asked to shorten it, which is not true of a post arriving over the air

#### Scenario: A post that was not confirmed
- **WHEN** a post arrives without the confirmation that view issued
- **THEN** nothing is stored, nothing is delivered, and the refusal is recorded as its own event
