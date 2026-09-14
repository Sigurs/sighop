## ADDED Requirements

### Requirement: A single flood advert can be requested explicitly
The system SHALL support emitting one flood advert for a named entity on explicit request,
submitted through the transmit scheduler as ordinary class 3 traffic with a deadline and charged
against the airtime budget like any other advert. Because a flood advert costs the same wherever
it came from, the system SHALL treat a requested flood as that entity's flood advert: the entity's
next scheduled flood SHALL move one jittered interval from the moment of the request, and the
request SHALL count as the most recent flood advert for the inter-entity gap. The system SHALL
make the time remaining in the inter-entity gap readable, so a caller can decline to flood inside
it. A requested flood SHALL NOT create or change an override, and SHALL NOT change the entity's
configured interval.

#### Scenario: One-shot flood advert
- **WHEN** a flood advert is explicitly requested for an entity
- **THEN** exactly one flood advert is submitted at priority class 3 with a deadline, charged against the budget, and the entity's count of adverts sent increases by one

#### Scenario: The request moves the flood schedule
- **WHEN** a flood advert is explicitly requested for an entity whose next scheduled flood is some hours away
- **THEN** its next scheduled flood is one jittered interval after the request, not at the earlier time, so the mesh does not receive two floods from it in quick succession

#### Scenario: The request counts toward the inter-entity gap
- **WHEN** a flood advert is explicitly requested and another local entity's scheduled flood falls due within the gap
- **THEN** that scheduled flood is deferred until the gap has elapsed, and the deferral is logged

#### Scenario: The gap remaining is readable
- **WHEN** a flood advert from any local entity was submitted less than the gap ago
- **THEN** the time remaining until the gap elapses is available to a caller, and is zero once the gap has elapsed or when no flood has been submitted this run

#### Scenario: A dropped requested flood is not retried
- **WHEN** a requested flood advert is dropped by the scheduler on deadline expiry
- **THEN** the drop is logged and no further flood is submitted for that entity before its next scheduled time
