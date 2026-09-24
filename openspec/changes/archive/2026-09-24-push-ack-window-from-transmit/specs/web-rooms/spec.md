# Spec Delta

## MODIFIED Requirements

### Requirement: Rooms the platform does not serve are shown as unserved
The system SHALL list every room the database holds on one rooms page, and SHALL distinguish the
rooms this run is serving from those it is not, giving the reason a room is unserved. The same list
SHALL carry each room's member and message counts, guest access, read-only fallback, retention and
delivery settings, and SHALL link each room's history, members, post, access, retention, delivery,
rename, delete and, for a served room, advert actions; the page SHALL also carry the form that
creates a room.

#### Scenario: A room whose identity is not loaded
- **WHEN** a room exists whose identity this run did not load
- **THEN** the room is listed as unserved with that reason, and its stored history is still browsable

#### Scenario: One list for reading and configuring
- **WHEN** the rooms page is opened on a run with a stored room
- **THEN** that room's row shows its counts, access, retention and delivery settings, and links both to its history and members and to its configuration actions

#### Scenario: Delivery settings at their defaults
- **WHEN** a room has neither delivery setting set
- **THEN** its row says plainly that it uses the firmware window and delivers to every member

#### Scenario: Returning after a room write
- **WHEN** a room's access, retention, delivery settings or name is changed, or a room is created or deleted
- **THEN** the operator is returned to the rooms page
