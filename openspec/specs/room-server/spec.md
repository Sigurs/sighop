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
