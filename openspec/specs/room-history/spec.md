# room-history Specification

## Purpose
What a room keeps and how members catch up on it — which posts are accepted, how history is
ordered and made durable, how each member's position is tracked and advanced, how catching up is
paced onto a shared channel, and what retention may remove.

## Requirements

### Requirement: A post from a permitted member is stored before it is acknowledged
The system SHALL treat a text message addressed to a room server from a member with posting
permission as a post, and SHALL acknowledge it **only after** it has been durably stored. Storage
SHALL NOT run on the path that receives and decodes packets, and SHALL be bounded by the sender's
own acknowledgement window; a post that cannot be stored within that window SHALL NOT be
acknowledged.

A post SHALL be accepted when its sender timestamp is no more than 300 seconds below the newest
sender timestamp recorded for that member, because a client stamps its posts and its logins and
keep-alives with different clocks. A post further below SHALL be refused as a replay, without an
acknowledgement, and the refusal SHALL state how far below the recorded timestamp it was. Whether
a post is a retry SHALL be decided by whether a post with the same author and sender timestamp is
already stored, not by comparing timestamps.

#### Scenario: A member posts
- **WHEN** a member with posting permission sends a text message to the room
- **THEN** the post is stored durably and only then acknowledged, and the acknowledgement is the one the sender computes for that exact transmitted plaintext

#### Scenario: Storage is unavailable
- **WHEN** a post arrives while durable storage is unavailable or too slow to complete inside the sender's acknowledgement window
- **THEN** nothing is acknowledged, nothing is stored, and the refusal is reported, so the sender reports a failure rather than believing a lost message was delivered

#### Scenario: A duplicate of an already-stored post
- **WHEN** the same post is retransmitted by its sender and recognised as a retry
- **THEN** it is acknowledged again and stored only once

#### Scenario: A post stamped by a clock behind the one that stamped the login
- **WHEN** a member's post carries a sender timestamp below the recorded one by 300 seconds or less, and no post with that timestamp is stored
- **THEN** it is stored and acknowledged, and the recorded timestamp is not lowered

#### Scenario: A retry of a stored post below the recorded timestamp
- **WHEN** a member retransmits a stored post whose sender timestamp is below the recorded one by 300 seconds or less
- **THEN** it is acknowledged again, reported as a retry, and stored only once

#### Scenario: A post far below the recorded timestamp
- **WHEN** a member's post carries a sender timestamp more than 300 seconds below the recorded one
- **THEN** it is refused as a replay, nothing is stored or transmitted, and the refusal states how many seconds below the recorded timestamp it was

### Requirement: A post carries its author, its text as received, and a length limit
The system SHALL record every post with its author's public key, the text exactly as it arrived
without lossy conversion, and the sender's own claimed timestamp alongside the room's. The system
SHALL keep at least as many bytes as a stock client can compose and display, and no more than the
push encoding can carry, so that nothing a client can legitimately send is shortened and everything
stored can be delivered. A post longer than that SHALL be shortened to it and still acknowledged,
and the acknowledgement SHALL be computed over the text as received rather than over what was kept,
so its sender stops retrying. Dropping any of an author's text SHALL be reported alongside the
post, naming the length as received.

A post submitted locally by an operator, whose author can be told, SHALL be refused rather than
shortened.

#### Scenario: A post whose text is not valid UTF-8
- **WHEN** a post arrives whose text is not valid UTF-8
- **THEN** the bytes are stored unchanged, and any rendering of them is marked as a rendering rather than presented as the author's text

#### Scenario: The longest post a stock client can compose
- **WHEN** a post arrives at the length a stock client's composer allows
- **THEN** it is stored whole, with nothing dropped and nothing reported as truncated

#### Scenario: An over-long post
- **WHEN** a post arrives longer than the maximum the room keeps
- **THEN** it is stored shortened to that maximum, acknowledged with the checksum its sender computed over the text it sent, and reported as truncated with the length it arrived at

#### Scenario: An over-long post from the operator
- **WHEN** an operator posts locally with text longer than the maximum
- **THEN** nothing is stored and the limit is reported, because the author is present to shorten it

### Requirement: History is durably ordered by a value the protocol can name
The system SHALL assign each post a room-scoped ordering value that is strictly increasing within
its room, never duplicated, and expressed in the same units the sync protocol carries on the wire,
so that a member's position in the history can be named by that value alone. Two posts made within
the same second SHALL receive distinct ordering values.

#### Scenario: Two posts in one second
- **WHEN** two posts are stored in the same second
- **THEN** they receive different ordering values and their relative order is preserved

#### Scenario: The clock steps backwards
- **WHEN** the system clock moves backwards between two posts
- **THEN** the ordering value still increases, and no post takes an ordering value already used in that room

#### Scenario: History survives a restart
- **WHEN** the process is restarted
- **THEN** every stored post is still present with its ordering value and author, and ordering continues from where it left off

### Requirement: Each member has a sync position that only advances on an acknowledgement
The system SHALL record for each member the ordering value up to which history has been confirmed
delivered, SHALL advance it only when the member acknowledges the delivery of a specific post, and
SHALL persist it so that a restart does not resend history the member already has or skip history
it does not.

#### Scenario: A push is acknowledged
- **WHEN** a member acknowledges a pushed post
- **THEN** its sync position advances to that post's ordering value and is persisted

#### Scenario: A push is never acknowledged
- **WHEN** a pushed post is not acknowledged within its window
- **THEN** the sync position does not advance and the post remains pending for that member

#### Scenario: A member returns after a restart
- **WHEN** a member that was behind returns after the process was restarted
- **THEN** delivery resumes from its stored position, covering everything it missed rather than only the most recent posts

#### Scenario: A member names its own position
- **WHEN** a member supplies a position in a login or a keep-alive request
- **THEN** that position is adopted for that member, so a client that has lost or reset its own history can resynchronise

### Requirement: History delivery is paced, one outstanding delivery per member, and never to the author
The system SHALL deliver unsynced history to members one post at a time per member, with at most
one delivery outstanding for a member at any moment, taking members in turn so that one member far
behind cannot exclude the others. A post SHALL NOT be delivered to its own author. A newly stored
post SHALL be held briefly before it becomes eligible for delivery. Delivery SHALL be submitted at
the reply priority class and remain subject to the duty-cycle ceiling and the transmit gate like all
other traffic.

#### Scenario: Two members are behind
- **WHEN** two members each have several unsynced posts
- **THEN** deliveries alternate between them rather than draining one member's backlog first

#### Scenario: An author's own post
- **WHEN** a member's own post becomes eligible for delivery
- **THEN** it is delivered to the other members and not back to its author

#### Scenario: Transmission is gated
- **WHEN** history delivery is due while transmission is not enabled
- **THEN** the deliveries are scheduled and reported as suppressed exactly as any other traffic, and no sync position advances

#### Scenario: The airtime ceiling is reached
- **WHEN** the duty-cycle ceiling is reached while history is being delivered
- **THEN** delivery yields to the ceiling like all other traffic and resumes when budget is available, rather than bypassing it

### Requirement: A member that cannot be reached is backed off rather than retried forever
The system SHALL bound the number of consecutive unacknowledged deliveries to one member before it
stops delivering to that member, SHALL resume when that member is next heard from, and SHALL report
how many members are in that state.

#### Scenario: A member goes out of range
- **WHEN** consecutive deliveries to a member go unacknowledged up to the bound
- **THEN** delivery to that member stops, its position is unchanged, and the state is reported

#### Scenario: The member returns
- **WHEN** that member is next heard from
- **THEN** delivery resumes from its unchanged position

### Requirement: Retention is a per-room policy that defaults to keeping everything
The system SHALL support a per-room retention policy bounding history by age, by count, or by both,
SHALL apply it with a periodic task rather than on the reception path, and SHALL default both bounds
to unset so that no message is ever deleted until an operator configures a policy.

#### Scenario: A room with no policy
- **WHEN** a room has no retention policy configured
- **THEN** no message is ever deleted, however old or numerous

#### Scenario: An age bound is set
- **WHEN** a room has an age bound and messages older than it exist
- **THEN** those messages are removed by the periodic task and the number removed is counted and reported

#### Scenario: A count bound is set
- **WHEN** a room has a count bound and holds more messages than it
- **THEN** the oldest messages beyond the bound are removed, and the newest are kept

### Requirement: Retention that outruns a member's sync position is reported, not hidden
The system SHALL report when retention removes messages a member had not yet received, so that a
gap in a member's history is visible as a consequence of policy rather than mistaken for a delivery
failure.

#### Scenario: A member is behind the retention window
- **WHEN** retention removes messages whose ordering values are above a member's sync position
- **THEN** the count of such messages is reported, and delivery to that member resumes from the oldest message that still exists

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
