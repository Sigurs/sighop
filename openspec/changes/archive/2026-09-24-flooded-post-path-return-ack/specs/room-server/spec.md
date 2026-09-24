# Spec Delta

## MODIFIED Requirements

### Requirement: A reply is routed the way the request arrived
The system SHALL answer a request that arrived flooded with a reply that also carries the route
back, so that the requester learns a path in the same exchange, and SHALL answer a request that
arrived directly along the member's known route, falling back to flooding only when no route is
known. A flood used to answer an authenticated request SHALL NOT require the operator flag that
governs originated traffic, and SHALL remain subject to the transmit gate, the priority classes and
the duty-cycle ceiling.

A post is a request for this purpose. A post that arrived flooded SHALL be acknowledged by a flooded
path return, encrypted to the member, that carries the path the post travelled and bundles the
post's acknowledgement. It SHALL NOT be acknowledged along a route learned from the post itself or
from overheard traffic. A post that arrived directly SHALL be acknowledged with a bare
acknowledgement along the member's known route, flooded only when no route is known. Either way the
acknowledgement is the one the sender computes for that exact transmitted plaintext. The route an
acknowledgement was sent on SHALL be reported with the stored post.

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

#### Scenario: A flooded post
- **WHEN** a member's post arrives as a flood and is stored
- **THEN** it is acknowledged by a flooded path return that carries the path the post travelled and bundles the acknowledgement the sender expects, even when a route to the member is already known

#### Scenario: A flooded post that arrived with no hops
- **WHEN** a member's post arrives as a flood that no repeater forwarded
- **THEN** it is still acknowledged by a flooded path return, carrying an empty path, not by a zero-hop direct acknowledgement

#### Scenario: A retried flooded post
- **WHEN** a member retransmits a flooded post that was already stored
- **THEN** it is acknowledged again by a flooded path return bundling the acknowledgement for that attempt, and nothing is stored twice

#### Scenario: A direct post
- **WHEN** a member's post arrives directly and the member's route is known
- **THEN** it is acknowledged with a bare acknowledgement along that route

#### Scenario: A direct post from a member with no known route
- **WHEN** a member's post arrives directly and no route to the member is known
- **THEN** it is acknowledged with a bare flooded acknowledgement

#### Scenario: Reporting how a post was acknowledged
- **WHEN** a post is stored and acknowledged
- **THEN** the report of the stored post names the route the acknowledgement took: a path return, a direct route with its hop count, or a flood

#### Scenario: Transmission is not enabled
- **WHEN** any reply is due while transmission is not enabled
- **THEN** it is scheduled and reported as suppressed, and no state that depends on delivery is advanced

#### Scenario: A request answered before the radio readback
- **WHEN** a request is answered before the board has answered its radio readback, and the readback arrives within the waiting budget
- **THEN** the reply is submitted and routed as it would have been otherwise, and nothing is refused

#### Scenario: A request answered when no readback ever comes
- **WHEN** a request is answered and no readback arrives within the waiting budget
- **THEN** the reply is refused and reported with a reason naming the expired wait, and the refusal is distinguishable from the silence an unauthorised request receives
