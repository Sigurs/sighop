# web-dashboard Specification

## Purpose

The instrument panel: what the platform is doing right now, shown densely enough that an operator
can watch it. Duty-cycle usage against the ceiling, the packet feed, queue depths, per-entity
counters, the contact table with its learned paths and signal quality, and the modem's own
readback — with the one rule the whole display rests on, that nothing unverified is ever drawn as
though it were verified.

## Requirements

### Requirement: Duty-cycle usage against the ceiling is always visible
The system SHALL present, on every page of the interface, the airtime consumed in the current
sliding window against the configured ceiling, expressed both as the remaining fraction and as the
consumed time, together with whether the ceiling in force is above the regulatory default and
whether transmission is enabled at all.

#### Scenario: The meter is present
- **WHEN** any page of the interface is displayed
- **THEN** the duty-cycle meter is on it, without navigation

#### Scenario: A raised ceiling is stated as raised
- **WHEN** the ceiling in force is above the regulatory default
- **THEN** the meter states that, rather than showing a proportion of a limit the operator cannot see

#### Scenario: The gate is closed
- **WHEN** transmission is disabled
- **THEN** the panel states that nothing will be transmitted, and the meter shows what would have been charged

### Requirement: The live packet feed streams receptions and transmissions as they happen
The system SHALL stream each decoded reception and each resolved transmission to a connected
browser as it occurs, carrying at minimum the direction, the time, the route type, the payload
type, the size, the path and its length, the signal quality where the receiver reported it, whether
the record was a duplicate, and the packet identifier that joins it to everything else about that
packet.

#### Scenario: A frame arrives
- **WHEN** a frame is decoded and dispatched
- **THEN** it appears in a connected browser's feed with its packet identifier and its decoded fields

#### Scenario: A transmission resolves
- **WHEN** a transmission completes, is dropped or expires
- **THEN** it appears in the feed with its outcome, distinguishable from a reception at a glance

#### Scenario: A frame that could not be decoded
- **WHEN** a frame arrives that cannot be decoded
- **THEN** it appears in the feed as unparsed, with its raw bytes and the reason, and is not omitted

### Requirement: The feed never delays or blocks the reception path
The system SHALL deliver the feed on a bounded queue per connection, SHALL discard records for a
connection that cannot keep up rather than waiting on it, and SHALL count the discards and report
them to that connection. Nothing in the reception, decode, dispatch or transmit path SHALL await a
browser.

#### Scenario: A browser that cannot keep up
- **WHEN** a connection's queue is full and another record arrives
- **THEN** the record is dropped for that connection, the drop is counted, and the platform's own handling of the record is unaffected

#### Scenario: Drops are visible
- **WHEN** records have been dropped for a connection
- **THEN** the interface shows that the feed is incomplete and how many records were lost

#### Scenario: A stalled connection
- **WHEN** a connection stops reading entirely
- **THEN** the platform continues to receive, decode and transmit at unchanged rates, and the connection is eventually closed and its event emitted

### Requirement: The feed is preceded by the recent history the browser missed
The system SHALL paint the feed on connection with the most recent recorded packets, newest first
and bounded in number, before streaming live records, and SHALL mark where the recorded history
ends and the live stream begins.

#### Scenario: Opening the panel on a running platform
- **WHEN** a browser connects to the feed on a platform that has been running
- **THEN** recent packets are shown immediately rather than an empty pane that fills at the mesh's own rate

#### Scenario: No recorded history available
- **WHEN** no database is configured or the recorded history cannot be read
- **THEN** the feed starts empty and says that only live records are shown

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

### Requirement: The contact table shows identity, route and signal together
The system SHALL present the known contacts with their name, public key, node hash, node type,
when each was first and last heard, and the learned routes for each — including an empty path
shown as a zero-hop direct route rather than as no route — with the hop count, the signal quality
and when the route was last confirmed.

#### Scenario: A zero-hop contact
- **WHEN** a contact's learned route has an empty path
- **THEN** it is displayed as a zero-hop direct route, distinct from a contact with no route at all

#### Scenario: A route found by node hash
- **WHEN** a route was matched by node hash rather than by public key
- **THEN** the display says the match is ambiguous

### Requirement: Unverified content is never presented as verified
The system SHALL visually distinguish content whose origin is cryptographically verified from
content that is not, and SHALL NOT present an unverified name, identity or claim in the same form
as a verified one. A contact whose advert signature has been verified SHALL be distinguishable
from one that has not. This is a display requirement, not a preference, and applies to every view.

#### Scenario: An unverified contact name
- **WHEN** a contact's advert signature has not been verified
- **THEN** its name is drawn distinctly from a verified contact's, and the view says what the distinction means

#### Scenario: Content whose sender is not authenticated
- **WHEN** a view shows a name or claim that the protocol does not authenticate
- **THEN** it is marked as unauthenticated wherever it appears, including in dense tabular views

#### Scenario: Colour is not the only signal
- **WHEN** verification status is conveyed
- **THEN** it is conveyed by more than colour alone

### Requirement: The feed summarises node discovery frames
The system SHALL show, in the live feed's detail column, a one-line summary of each node discovery
frame received live: for a request, the tag and the node types the filter selects; for a
response, the tag, the responder node type, the SNR it reports and a claimed key prefix. The
claimed key SHALL be labelled as unauthenticated, per the requirement that unverified content is
never presented as verified. A discovery frame replayed from recorded history SHALL still show
its discovery outcome. The summary is live-only, because it is not persisted.

#### Scenario: A discovery response arrives while the feed is open
- **WHEN** a discovery response is received with a browser connected
- **THEN** its feed row shows the outcome `discover_response` and a detail summary carrying the tag, the node type, the reported SNR and a key prefix labelled as unauthenticated

#### Scenario: A discovery request and its responses
- **WHEN** a discovery request and a response to it are received in turn
- **THEN** both rows show the same tag, so the pairing can be read from the feed

#### Scenario: A discovery frame in recorded history
- **WHEN** the feed paints recorded history containing a discovery response
- **THEN** the row shows the outcome `discover_response`, not `uninterpreted_payload`

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
