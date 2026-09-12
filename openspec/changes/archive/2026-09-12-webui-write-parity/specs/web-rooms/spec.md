## ADDED Requirements

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
