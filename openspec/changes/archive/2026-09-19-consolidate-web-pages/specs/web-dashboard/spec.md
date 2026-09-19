## ADDED Requirements

### Requirement: Navigation names each concern once
The system SHALL offer, on every page, navigation to exactly these pages: overview, contacts, chat,
rooms, identities, webhooks and system. Each concern SHALL be presented on one of them rather than
split across two: rooms are read and configured on the rooms page, channels are read and
administered from chat, a bot is configured on its identity's page, and the board's readback and
the schema revision are on the system page. The former pages for modem health, radio, schema, room
configuration, bots and channels SHALL NOT be served.

#### Scenario: The navigation
- **WHEN** any page is opened by a signed-in operator
- **THEN** its navigation links to overview, contacts, chat, rooms, identities, webhooks and system, and to nothing else

#### Scenario: A former page
- **WHEN** a former page for modem health, radio, schema, room configuration, bots or channels is requested
- **THEN** it is not found, and no page links to it

## MODIFIED Requirements

### Requirement: Platform counters and modem health are presented
The system SHALL present, on the overview, the transmit queue depth by priority class, the
scheduler's admitted, transmitted, dropped and expired counts, the deduplication cache's occupancy
and duplicate count, the count of learned path destinations and the number of contacts; SHALL
present the persistence state with its discarded-write counts in the strip shown on every page; and
SHALL present the modem's own reported parameters and health, as the startup probe answered them,
on the system page.

#### Scenario: The modem did not answer a query
- **WHEN** the board did not answer a probe query
- **THEN** that value is shown on the system page as absent, never as zero or as a default

#### Scenario: Persistence is degraded
- **WHEN** the platform reports a degraded database
- **THEN** the strip on every page shows that state and the counts of writes discarded while it has been degraded
