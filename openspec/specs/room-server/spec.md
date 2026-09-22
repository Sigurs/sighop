# room-server Specification

## Purpose
The room-server entity itself — how a room is bound to an identity and advertised to the mesh,
which local entity owns an inbound packet, how requests for keep-alive, status and telemetry are
answered, and what a run reports about the rooms it serves.

## Requirements

### Requirement: A room is bound to exactly one stored identity, which adverts as a room server
The system SHALL bind a room to exactly one stored entity, SHALL advertise that entity with the
room-server node type on the same advert schedule and under the same advert policy as any other
entity, and SHALL refuse to bind a second room to an identity that already has one.

#### Scenario: A room server adverts
- **WHEN** a room server entity's advert is due
- **THEN** the advert carries the room-server node type and is submitted through the transmit scheduler like any other advert, subject to the advert floor and the duty-cycle ceiling

#### Scenario: Binding a second room to one identity
- **WHEN** a room is created on an identity that already has one
- **THEN** it is refused with a message naming the existing room, and nothing is changed

#### Scenario: No durable storage is configured
- **WHEN** the runtime starts with no database configured
- **THEN** no rooms exist, no room server is served, and the startup output states that rooms require durable storage rather than silently serving nothing

### Requirement: An entity that serves a room owns the packets addressed to it
The system SHALL handle traffic addressed to a room server entity's node hash as room-server
traffic, and SHALL NOT also handle it as an ordinary direct message, so that exactly one component
decrypts a given packet and at most one acknowledgement is transmitted for it.

#### Scenario: A member posts to a room
- **WHEN** a text message addressed to a room server entity is received
- **THEN** it is decrypted once, handled as a post, and acknowledged once

#### Scenario: A direct message to a non-room entity
- **WHEN** a text message addressed to an entity that serves no room is received
- **THEN** it is handled as an ordinary direct message exactly as before

### Requirement: A reply is routed the way the request arrived
The system SHALL answer a request that arrived flooded with a reply that also carries the route
back, so that the requester learns a path in the same exchange, and SHALL answer a request that
arrived directly along the member's known route, falling back to flooding only when no route is
known. A flood used to answer an authenticated request SHALL NOT require the operator flag that
governs originated traffic, and SHALL remain subject to the transmit gate, the priority classes and
the duty-cycle ceiling.

A reply SHALL NOT be lost to a radio readback that has not arrived yet. Where a request is answered
before the board has answered its readback, the reply SHALL wait for those parameters within a
bounded budget and then be submitted. A client whose login or keep-alive falls in a run's first
moments is answered, not met with the silence this specification reserves for unauthorised requests.

#### Scenario: A flooded login
- **WHEN** a successful login arrived as a flood
- **THEN** the reply carries both the login result and the path back to the room server, so the client can address it directly afterwards

#### Scenario: A direct request from a member with a known route
- **WHEN** a request arrives directly from a member whose route is known
- **THEN** the reply is sent along that route rather than flooded

#### Scenario: Transmission is not enabled
- **WHEN** any reply is due while transmission is not enabled
- **THEN** it is scheduled and reported as suppressed, and no state that depends on delivery is advanced

#### Scenario: A request answered before the radio readback
- **WHEN** a request is answered before the board has answered its radio readback, and the readback arrives within the waiting budget
- **THEN** the reply is submitted and routed as it would have been otherwise, and nothing is refused

#### Scenario: A request answered when no readback ever comes
- **WHEN** a request is answered and no readback arrives within the waiting budget
- **THEN** the reply is refused and reported with a reason naming the expired wait, and the refusal is distinguishable from the silence an unauthorised request receives

### Requirement: A successful login is answered with the login result the reference implementation defines
The system SHALL answer a successful login with a response carrying the server's current time, a
success indicator, the admitted permission level, and the protocol level it implements, in the
field layout the reference implementation defines, so that a stock client displays the login as
successful and knows what it is permitted to do.

#### Scenario: An administrator logs in
- **WHEN** a login is admitted with administrator permission
- **THEN** the response identifies the member as an administrator and carries its permission level

#### Scenario: A read-only member logs in
- **WHEN** a login is admitted with read-only permission
- **THEN** the response identifies the member as a spectator and carries its permission level

### Requirement: A keep-alive request is answered with an acknowledgement carrying the unsynced count
The system SHALL answer a keep-alive request from a member with an acknowledgement computed over
that request, extended with the number of messages that member has yet to receive, and SHALL adopt
a sync position supplied with the request. A keep-alive SHALL be answered only along a known route.

#### Scenario: A member sends a keep-alive
- **WHEN** a member sends a keep-alive request over a known route
- **THEN** it is acknowledged, the acknowledgement carries the member's unsynced count, and the member's activity is recorded

#### Scenario: A keep-alive supplying a position
- **WHEN** a keep-alive carries a sync position
- **THEN** the member's position is set to it, so a client that reset its history resynchronises from there

#### Scenario: A keep-alive with no known route back
- **WHEN** a keep-alive arrives from a member whose route is unknown
- **THEN** nothing is transmitted, because a keep-alive answer is only meaningful over a route

### Requirement: A status request is answered with what the platform actually knows
The system SHALL answer a status request from a member with the room server's statistics in the
field layout the reference room-server implementation defines, SHALL report counters that describe
the shared radio as covering the whole runtime rather than one entity, SHALL report post and
delivery counters per room, and SHALL NOT invent a value for a measurement the hardware does not
provide.

#### Scenario: A member requests status
- **WHEN** a member requests status
- **THEN** the answer echoes the request's timestamp as a tag and carries the statistics in the layout the reference room-server implementation defines

#### Scenario: A measurement the modem cannot provide
- **WHEN** a statistic has no equivalent on a KISS modem
- **THEN** it is reported as absent in the way the wire format expresses absence, and the documentation for the answer says which fields those are

#### Scenario: Several entities share one radio
- **WHEN** the runtime serves more than one entity
- **THEN** the radio counters in the answer describe the whole runtime, and the post and delivery counters describe only the room that was asked

### Requirement: A telemetry request is answered with the values the board reported, and no others
The system SHALL answer a telemetry request from a member with a telemetry frame carrying the
values the board reported for itself, SHALL omit any value the board did not answer rather than
substituting a default, and SHALL state that it exposes no external sensors, so that the request's
sensor permission mask has nothing to gate.

#### Scenario: The board answered the battery query
- **WHEN** a member requests telemetry and the board reported a battery voltage
- **THEN** the frame carries that voltage on the device's own channel

#### Scenario: The board did not answer a query
- **WHEN** the board did not answer the temperature query
- **THEN** the frame omits temperature rather than carrying a placeholder value

#### Scenario: A guest requests telemetry
- **WHEN** a member with the lowest permission level requests telemetry
- **THEN** it receives the same base frame, because no external sensor values exist to be gated

### Requirement: An unsupported or unauthorised request is answered with silence
The system SHALL NOT transmit anything in response to a request type it does not implement, or to a
request a member's permission level does not allow, and SHALL report the request and the reason it
went unanswered.

#### Scenario: An unimplemented request type
- **WHEN** a member sends a request type the system does not implement
- **THEN** nothing is transmitted, and the request type and its sender are reported

### Requirement: A run reports the rooms it serves
The system SHALL report at startup each room it is serving, its identity, its membership count, how
many messages it holds and its retention policy, and SHALL include in periodic status output the
room counters that change — messages stored, deliveries outstanding, members behind, logins refused
by reason, and messages removed by retention.

#### Scenario: A run with rooms starts
- **WHEN** the runtime starts with one or more rooms
- **THEN** the startup output names each room, its identity, its members, its message count and its retention policy, stating plainly when retention is unlimited

#### Scenario: Periodic status while serving a room
- **WHEN** a periodic status line is produced while rooms are being served
- **THEN** it carries the room counters, so a room that is refusing logins or failing to deliver is visible without reading the event stream

### Requirement: A push waits for the radio readback rather than being dropped
The system SHALL, where a push to a member is composed before the board has answered its radio
readback, wait for those parameters within a bounded budget and then submit the push, rather than
refusing it outright. A push carries a room's messages to a member that is owed them, and dropping
one at startup loses the delivery without telling the member anything is missing.

#### Scenario: A push composed before the readback
- **WHEN** a push is composed before the board has answered its radio readback, and the readback arrives within the waiting budget
- **THEN** the push is submitted, and the member's unsynced state advances exactly as it would have otherwise

#### Scenario: A push composed when no readback ever comes
- **WHEN** a push is composed and no readback arrives within the waiting budget
- **THEN** the push is refused and recorded with a reason naming the expired wait, and no state that depends on delivery is advanced

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

### Requirement: A room created while a run is active is served without a restart

The system SHALL serve a room created while a run is active, without being restarted, provided that
run holds the identity the room is bound to and that identity is enabled. A room served this way
SHALL be indistinguishable from one bound at startup: it SHALL own the packets addressed to its
identity, answer login, keep-alive, status and telemetry requests under the same rules, route
replies the way the request arrived, and be counted in periodic status output alongside the rooms
the run started with.

Where the run does not hold the room's identity — it is not loaded, or it is not enabled — the
system SHALL NOT serve the room and SHALL state the same reason it states at startup for a room it
does not serve. Where that identity is later adopted, the room SHALL then be served without a
restart, so that creating an identity and a room in either order reaches the same state.

Taking up a room SHALL NOT disturb the rooms already served, and SHALL NOT transmit anything of
itself.

#### Scenario: A room created from the command line

- **WHEN** a room is created on an identity a run holds while that run is active
- **THEN** that run serves it without a restart, and answers requests addressed to its identity

#### Scenario: A room created through this run's own interface

- **WHEN** a room is created through the web interface of the running process
- **THEN** that run serves it without waiting for the periodic re-read and without a restart

#### Scenario: A room on an identity this run does not hold

- **WHEN** a room is created on a stored identity that is not loaded by this run
- **THEN** the room is not served, and the run states that its identity was not loaded, exactly as it does at startup

#### Scenario: The identity arrives after the room

- **WHEN** a room is created on an identity this run does not hold, and that identity is later adopted by the run
- **THEN** the room is then served without a restart

#### Scenario: The rooms already served are undisturbed

- **WHEN** a new room is taken up mid-run
- **THEN** every room already served keeps its members, its unsynced positions and its counters, and no member is logged out

### Requirement: A room stops being served when its identity stops being held

The system SHALL stop serving a room whose bound identity is disabled or removed while the run is
active, without being restarted, and SHALL answer no login, keep-alive, status or telemetry request
addressed to it. The room, its members and its stored messages SHALL be unaffected: an identity
being disabled is not a deletion, and the room SHALL be served again, with its membership and
history intact, if that identity is enabled again.

The system SHALL state that it has stopped serving the room and why, because a room that has gone
silent because its identity was disabled must be distinguishable from one that has failed.

#### Scenario: The room's identity is disabled

- **WHEN** the identity a served room is bound to is disabled while the run is active
- **THEN** the run stops serving that room without a restart, answers no request addressed to it, and states the reason

#### Scenario: The room and its history survive

- **WHEN** a room stops being served because its identity was disabled
- **THEN** the room, its members and its stored messages are unchanged, and it is listed as stored but not served

#### Scenario: The identity is enabled again

- **WHEN** the identity of a room that stopped being served is enabled again
- **THEN** the run serves that room again without a restart, with its membership and message history intact
