## MODIFIED Requirements

### Requirement: Routing prefers a known zero-hop route and never floods by default
The system SHALL send a direct message as `DIRECT` using a learned route to the recipient when
one is known, SHALL send it with an empty path when the learned route is zero-hop, and SHALL
refuse to send when no route is known unless flooding has been explicitly permitted for that
invocation. When a preferred first hop is set, the learned route SHALL be resolved as the
`route-preference` capability defines before it is sent, and the acknowledgement timeout SHALL be
computed from the hop count of the route actually sent.

#### Scenario: A zero-hop route is known
- **WHEN** a message is sent to a contact for which a zero-hop route was learned and no preferred first hop is set
- **THEN** the packet is `DIRECT` with an empty path

#### Scenario: A zero-hop route is known and a preferred first hop is set
- **WHEN** a message is sent to a contact for which a zero-hop route was learned while a different repeater is the preferred first hop
- **THEN** the packet is `DIRECT` with one hop through the preferred repeater, its acknowledgement timeout is the one-hop direct timeout, and the output marks the route as through the preferred first hop

#### Scenario: No route is known and flooding was not permitted
- **WHEN** a message is sent to a contact with no learned route and no explicit flood permission
- **THEN** the send fails with an error saying no route is known, and no packet is queued

#### Scenario: No route is known and flooding was permitted
- **WHEN** a message is sent to a contact with no learned route and flooding explicitly permitted
- **THEN** the packet is sent `FLOOD`, and the output states that it was flooded
