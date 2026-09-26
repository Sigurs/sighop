## MODIFIED Requirements

### Requirement: The contact table shows identity, route and signal together
The system SHALL present the known contacts with their name, public key, node hash, node type,
when each was first and last heard, and the learned routes for each — including an empty path
shown as a zero-hop direct route rather than as no route — with the hop count, the signal quality
and when the route was last confirmed. The public key SHALL be shown abbreviated with the full key
copyable, and the route as a hop glyph with its count and path, as the `web-display` conventions
require. The route shown SHALL be the route a send would actually use, including any rewrite by
the preferred first hop, and a rewritten route SHALL be marked as such.

#### Scenario: A zero-hop contact
- **WHEN** a contact's learned route has an empty path and no preferred first hop is set
- **THEN** it is displayed as a zero-hop direct route, distinct from a contact with no route at all

#### Scenario: A route found by node hash
- **WHEN** a route was matched by node hash rather than by public key
- **THEN** the display says the match is ambiguous

#### Scenario: A contact's public key
- **WHEN** the contact table is displayed
- **THEN** each public key shows its first three bytes, and its copy control copies the full key

#### Scenario: A route rewritten through the preferred first hop
- **WHEN** a preferred first hop is set and a contact's learned route is zero-hop
- **THEN** the route is displayed as one hop through the preferred repeater and marked as rewritten by the preferred first hop
