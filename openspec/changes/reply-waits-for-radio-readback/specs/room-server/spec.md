## MODIFIED Requirements

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

## ADDED Requirements

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
