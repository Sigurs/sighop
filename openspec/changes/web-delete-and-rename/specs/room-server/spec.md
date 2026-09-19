# Spec Delta

## ADDED Requirements

### Requirement: A room can be deleted, and what goes with it is counted first
The system SHALL provide an explicit action that deletes a room. Deleting a room SHALL delete its
members and its stored messages with it, because a membership and a message are meaningful only
under the room that holds them. The system SHALL state, before the deletion is applied, how many
members and how many stored messages it will delete.

Deletion SHALL be irreversible and SHALL say so before it happens. It SHALL require a deliberate
confirmation of that specific room, and SHALL NOT be performed by a request that could be issued
incidentally.

Deleting a room SHALL NOT delete the identity the room was bound to. That identity SHALL remain
stored, keep its key material, and become unbound, so that it can carry a new room or be removed
in its own right. A run serving the deleted room SHALL stop serving it without being restarted,
and SHALL NOT answer a login, keep-alive, status or telemetry request addressed to it.

#### Scenario: Deleting a room
- **WHEN** a room is deleted with confirmation
- **THEN** the room, its members and its stored messages are gone, and the room no longer appears in the listing

#### Scenario: The cost is stated first
- **WHEN** deletion is offered for a room holding members and messages
- **THEN** the number of members and the number of stored messages that will be deleted are stated before the deletion is applied

#### Scenario: Deletion without confirmation
- **WHEN** deletion is asked for without the confirmation for that room
- **THEN** nothing is deleted and the refusal says the action was not confirmed

#### Scenario: The identity survives
- **WHEN** a room is deleted
- **THEN** the identity it was bound to is still stored with its key material and its type unchanged, and is reported as serving no room

#### Scenario: The identity can carry a new room
- **WHEN** a room is deleted and a new room is created on the same identity
- **THEN** the new room is created, because the identity no longer carries one

#### Scenario: A running process stops serving it
- **WHEN** a room served by a running process is deleted
- **THEN** that process stops serving it without a restart and answers no request addressed to it

### Requirement: A room's name is validated wherever it is set
The system SHALL apply one validation to a room's name at every point that sets it — creating and
renaming — so that a name the store accepts is a name every surface can use. The validation SHALL
refuse a name that is empty or only whitespace, one longer than the limit the store sets, and one
containing a control or unassigned character. The refusal SHALL name the reason and SHALL be the
same reason whichever surface asked, nothing SHALL be stored or changed when a name is refused,
and a name SHALL be stored with surrounding whitespace stripped.

#### Scenario: Creating a room with an empty name
- **WHEN** a room is created with an empty or whitespace-only name
- **THEN** the creation is refused saying a name cannot be empty, and no room is stored

#### Scenario: A control character in a room name
- **WHEN** a room name containing a control or unassigned character is submitted at create or at rename
- **THEN** it is refused naming the code point, and nothing is stored or changed

#### Scenario: The same reason from either surface
- **WHEN** the same refused room name is submitted through the command line and through the interface
- **THEN** both give the same reason

#### Scenario: Surrounding whitespace
- **WHEN** a room is created or renamed with a name carrying leading or trailing whitespace
- **THEN** the stored name has that whitespace stripped

### Requirement: A room can be renamed, and its name is a local label
The system SHALL provide an action that changes a room's name and nothing else. A room's name
SHALL NOT be carried in a login response and SHALL NOT be advertised, so a rename SHALL have no
effect visible to a member or to the mesh: the room is the same node at the same public key, and
members stay members.

The system SHALL refuse a rename whose new name is empty or only whitespace, and SHALL refuse a
rename to a name another room already holds, because rooms are addressed by name on the command
line. A rename to the name the room already holds SHALL be accepted and change nothing.

#### Scenario: Renaming a room
- **WHEN** a room is renamed
- **THEN** it is listed under its new name, and its identity, members, messages, passwords and retention bounds are unchanged

#### Scenario: A rename is not visible to the mesh
- **WHEN** a room served by a running process is renamed and a member then logs in
- **THEN** the login is answered as before, because a room's name is not carried in the answer

#### Scenario: Renaming to a name already in use
- **WHEN** a rename is asked for with a name another room already holds
- **THEN** the rename is refused and nothing is changed

#### Scenario: Renaming to an empty name
- **WHEN** a rename is asked for with an empty or whitespace-only name
- **THEN** the rename is refused and nothing is changed
