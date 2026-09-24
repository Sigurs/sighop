# Spec Delta

## ADDED Requirements

### Requirement: A push's acknowledgement window starts once it has been transmitted
The system SHALL measure a pushed post's acknowledgement window from the moment the push has
finished transmitting, not from the moment it was composed or submitted, so that time spent waiting
to transmit and the push's own time on air are not taken from the member's time to answer. A push
that has not yet finished transmitting SHALL NOT time out, SHALL NOT count as an unacknowledged
delivery, and SHALL NOT be retried. The length of the window SHALL be unchanged: a flat window for
a flooded push and a window that grows with hop count for a direct one.

#### Scenario: A push waits behind other traffic
- **WHEN** a push is held in the transmit queue for longer than its acknowledgement window before it is transmitted, and the member acknowledges it within the window after transmission
- **THEN** the delivery is acknowledged, the member's sync position advances, and the push is not retried

#### Scenario: A push takes a long time on air
- **WHEN** a push's own time on air is a large share of its acknowledgement window
- **THEN** the member still has the full window, counted from the end of transmission, to acknowledge it

#### Scenario: No acknowledgement after transmission
- **WHEN** a push finishes transmitting and no acknowledgement arrives within its window after that
- **THEN** the delivery times out, counts once toward the member's consecutive failures, and the post remains pending for that member

#### Scenario: A push that is never transmitted
- **WHEN** a push is suppressed, dropped or fails to transmit
- **THEN** no acknowledgement window is started, it does not count toward the member's failures, and the post remains pending for that member

### Requirement: A late acknowledgement of an earlier attempt still confirms the post
The system SHALL treat an acknowledgement that matches any earlier attempt of the post currently
being delivered to a member as confirming delivery of that post, even after that attempt's window
has expired and a retry has been composed or transmitted. Confirming a post this way SHALL advance
and persist the member's sync position exactly as an on-time acknowledgement does, SHALL reset the
member's consecutive failure count, and SHALL end any retry of that post still outstanding for the
member. The system SHALL stop accepting an earlier attempt's acknowledgement once that post has been
confirmed or the member's pending post is no longer that post.

#### Scenario: The acknowledgement arrives while the retry is being sent
- **WHEN** a push times out, a retry of the same post is submitted, and the member's acknowledgement of the first attempt then arrives
- **THEN** the post is confirmed, the member's sync position advances to it, the retry is no longer outstanding, and the acknowledgement is not reported as unmatched

#### Scenario: A late acknowledgement after failures
- **WHEN** a member has consecutive unacknowledged deliveries of a post and an acknowledgement of one of those earlier attempts arrives
- **THEN** the member's failure count returns to zero, and a member that had been backed off resumes delivery from its new position

#### Scenario: A stale acknowledgement after the post was confirmed
- **WHEN** a post has been confirmed and an acknowledgement of another attempt of that same post arrives afterwards
- **THEN** the sync position does not move again and nothing else is delivered or skipped because of it

#### Scenario: Reporting a late acknowledgement
- **WHEN** a delivery is confirmed
- **THEN** the report of it states how long after transmission the acknowledgement arrived and whether it matched an earlier attempt rather than the most recent one

### Requirement: A room's push acknowledgement window can be set by the operator
The system SHALL let each room carry an optional push acknowledgement window, in whole seconds,
between 5 and 300 inclusive. When it is unset, a push's window SHALL be the firmware's: a flat
window for a flooded push and one that grows with hop count for a direct push. When it is set, every
push from that room SHALL use that window, flooded or direct, counted from the end of transmission.
A room SHALL be created with it unset, and a change to it SHALL apply to pushes composed after the
change without a restart, leaving a push already outstanding on the window it started with.

#### Scenario: A room with no window set
- **WHEN** a room has no push acknowledgement window set
- **THEN** its pushes use the firmware's flooded and direct windows exactly as before

#### Scenario: A room with a window set
- **WHEN** a room's push acknowledgement window is set to 30 seconds
- **THEN** a flooded push and a direct push over any number of hops from that room each wait 30 seconds after transmission for their acknowledgement

#### Scenario: A value out of range
- **WHEN** a push acknowledgement window below 5, above 300, or not a whole number is submitted
- **THEN** it is refused naming the allowed range, and the stored window is unchanged

### Requirement: A room can limit history delivery to members heard recently
The system SHALL let each room carry an optional recency limit, in whole days, of at least 1. When
it is set, the system SHALL NOT push history to a member that has not been heard within that many
days, where a member is heard when it logs in, keeps alive, posts, or sends any other request to the
room, or when an advert from its identity is received. When it is unset, every member SHALL be
eligible exactly as before. A member skipped for recency SHALL keep its sync position, SHALL NOT
accrue a delivery failure, and SHALL become eligible again on the next delivery round after it is
heard. The number of members a room is currently skipping for recency SHALL be reported with the
room's other counters.

#### Scenario: A member not heard within the limit
- **WHEN** a room's recency limit is 7 days and a member behind in history was last heard 10 days ago
- **THEN** nothing is pushed to that member, its sync position and failure count are unchanged, and it is counted as skipped for recency

#### Scenario: The member is heard again
- **WHEN** that member logs in, or an advert from its identity is received
- **THEN** it becomes eligible on the next round and delivery resumes from its stored position

#### Scenario: A member heard only by advert
- **WHEN** a member has not interacted with the room within the limit but an advert from its identity was received within it
- **THEN** the member is eligible for delivery

#### Scenario: No limit set
- **WHEN** a room has no recency limit
- **THEN** every member behind in history is eligible for delivery, however long ago it was heard
