# route-preference Specification

## Purpose
Lets an operator name one nearby repeater that every DIRECT send leaves through first, so a node
with a weak antenna reaches the mesh through a neighbour with a strong one.
## Requirements
### Requirement: One station-wide preferred first hop, or none
The system SHALL hold at most one preferred first hop for the whole node, identified by the
repeater's public key. It SHALL NOT vary by identity, room, bot or channel. With none set, every
route chosen and every packet sent SHALL be identical to the behaviour before this capability
existed. The setting SHALL be stored in the database, restored at startup before traffic is sent,
and a change SHALL apply to the running process without a restart.

#### Scenario: None set
- **WHEN** no preferred first hop is set and a direct message is sent along a learned route
- **THEN** the packet is byte-identical to the one sent before this capability existed

#### Scenario: Restart with a preferred first hop stored
- **WHEN** the process restarts with a preferred first hop stored
- **THEN** the first DIRECT send after startup already goes through it

#### Scenario: Changed while running
- **WHEN** the preferred first hop is changed on the system page
- **THEN** the next route chosen uses the new setting, with no restart

### Requirement: A learned route already leaving through the preferred repeater wins
When a preferred first hop is set, the system SHALL resolve a destination's route to the most
recently confirmed candidate whose first hop equals the preferred repeater's hash at that
candidate's hash width, and SHALL fall back to the most recently confirmed candidate of all only
when no candidate starts with it. The candidate is used unchanged.

#### Scenario: An older candidate starts with the preferred repeater
- **WHEN** a destination has a zero-hop candidate confirmed a minute ago and a one-hop candidate through the preferred repeater confirmed an hour ago
- **THEN** the one-hop candidate through the preferred repeater is chosen and sent as learned

#### Scenario: Multi-byte hashes
- **WHEN** a candidate learned at hash width 2 starts with the first 2 bytes of the preferred repeater's public key
- **THEN** it counts as starting with the preferred repeater

### Requirement: Otherwise the preferred repeater is prepended
When a preferred first hop is set and no candidate starts with it, the system SHALL prepend the
preferred repeater's hash, at the chosen candidate's hash width, to the chosen candidate's path and
increase its hop count by one. A zero-hop route SHALL become a one-hop route through the preferred
repeater. The node-hash ambiguity of the underlying route SHALL be kept.

#### Scenario: Zero-hop route
- **WHEN** the only candidate for a destination is zero-hop and the preferred repeater's key starts `ab cd ef`
- **THEN** the packet is sent DIRECT with hop count 1 and path `ab`, `ab cd` or `ab cd ef` according to the width the zero-hop route was learned at

#### Scenario: Two-hop route through other repeaters
- **WHEN** the chosen candidate is the 2-hop path `11 22` at width 1 and the preferred repeater's hash is `ab`
- **THEN** the packet is sent DIRECT with hop count 3 and path `ab 11 22`

#### Scenario: Ambiguous route rewritten
- **WHEN** the chosen candidate was found by node hash and is therefore ambiguous
- **THEN** the rewritten route is still reported as ambiguous

### Requirement: A route through the preferred repeater further along is shortened, not looped
When the chosen candidate's path contains the preferred repeater's hash at a position other than
the first, the system SHALL send the path from that position onward instead of prepending, so the
preferred repeater appears once and first.

#### Scenario: Preferred repeater in the middle
- **WHEN** the chosen candidate is `11 ab 22` at width 1 and the preferred repeater's hash is `ab`
- **THEN** the packet is sent DIRECT with hop count 2 and path `ab 22`

### Requirement: Exemptions and limits
The system SHALL NOT rewrite the route to the preferred repeater itself. The system SHALL send the
chosen candidate unchanged when prepending would exceed the protocol's path limits (64 path bytes,
63 hops), and SHALL count each such fallback. The system SHALL NOT alter floods, and SHALL NOT
alter the path of a packet that echoes or follows a route taken from a received frame rather than
from the learned route store.

#### Scenario: Sending to the preferred repeater
- **WHEN** a packet is addressed to the preferred repeater along a zero-hop route
- **THEN** it is sent zero-hop with an empty path

#### Scenario: Path at the limit
- **WHEN** the chosen candidate already has 21 hops at width 3
- **THEN** it is sent unchanged and the fallback count increases by one

#### Scenario: No route known
- **WHEN** no route is known to a destination and flooding is permitted
- **THEN** the packet is flooded exactly as without a preferred first hop

### Requirement: Every sender resolves routes the same way
Direct messages, their acknowledgements sent DIRECT, room-server sends along a member's learned
route, and repeater-collection polls SHALL all resolve their route through the same preference, so
no component sends along a route another would not.

#### Scenario: Room reply to a zero-hop member
- **WHEN** a room server sends to a member with a learned zero-hop route while a preferred first hop is set
- **THEN** the packet is sent DIRECT with one hop through the preferred repeater

#### Scenario: Polling a repeater other than the preferred one
- **WHEN** repeater collection polls a repeater with a learned zero-hop route while a different repeater is preferred
- **THEN** the poll is sent DIRECT with one hop through the preferred repeater

### Requirement: A rewritten route is reported as rewritten
Wherever a route is reported — send output, logs, the contact table — the system SHALL mark a route
that was prepended or shortened as going through the preferred first hop, distinct from a route
used as learned.

#### Scenario: Send output
- **WHEN** a direct message is sent along a prepended route
- **THEN** the output names the route type and hop count and marks it as through the preferred first hop
