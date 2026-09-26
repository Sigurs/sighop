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
- **WHEN** the recorded history cannot be read
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

### Requirement: Unverified content is never presented as verified
The system SHALL visually distinguish content whose origin is cryptographically verified from
content that is not, and SHALL NOT present an unverified name, identity or claim in the same form
as a verified one. A contact whose advert signature has been verified SHALL be distinguishable
from one that has not. The marking MAY be a glyph without an accompanying word, provided each
verification state and the unverified-claim marking have distinct glyphs and the meaning of each is
available on hover, to assistive technology, and in a legend on any page that draws them. This is a
display requirement, not a preference, and applies to every view.

#### Scenario: An unverified contact name
- **WHEN** a contact's advert signature has not been verified
- **THEN** its name is drawn distinctly from a verified contact's, and the view says what the distinction means

#### Scenario: Content whose sender is not authenticated
- **WHEN** a view shows a name or claim that the protocol does not authenticate
- **THEN** it is marked as unauthenticated wherever it appears, including in dense tabular views

#### Scenario: Colour is not the only signal
- **WHEN** verification status is conveyed
- **THEN** it is conveyed by more than colour alone

#### Scenario: Hovering a verification mark
- **WHEN** the operator hovers a verified, unverified, key-only or claimed-name mark
- **THEN** a statement of what that mark means is shown

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
configuration, bots and channels SHALL NOT be served. The navigation SHALL mark which of those
pages is being viewed, both visibly by something other than colour alone and to assistive
technology, including on a page reached beneath one of them.

#### Scenario: The navigation
- **WHEN** any page is opened by a signed-in operator
- **THEN** its navigation links to overview, contacts, chat, rooms, identities, webhooks and system, and to nothing else

#### Scenario: A former page
- **WHEN** a former page for modem health, radio, schema, room configuration, bots or channels is requested
- **THEN** it is not found, and no page links to it

#### Scenario: The page being viewed
- **WHEN** the contacts page is opened
- **THEN** its navigation marks contacts as the current page, distinguishably without colour and to assistive technology, and marks no other

#### Scenario: A page beneath a navigation entry
- **WHEN** a conversation under chat is opened
- **THEN** the navigation marks chat as the current page

### Requirement: Repeaters are selected for collection from the contact table
The contact table SHALL show, for each repeater contact, a "Collect metrics" checkbox reflecting
whether that repeater is selected for collection, and changing it SHALL store the selection without
a page reload and without a restart. Contacts that are not repeaters SHALL have no checkbox. For
each selected repeater the table SHALL show when it was last polled and that poll's outcome, and
link to its metrics page. A selected repeater outside the recency window SHALL be marked as
skipped for that reason. A repeater whose latest recorded status estimates its battery below 50%
SHALL be marked as low on battery, with the voltage, the estimate and when that status was
collected; a repeater whose latest status senses no battery SHALL carry no battery mark. Selection
SHALL NOT be offered when the run has no database.

#### Scenario: Ticking a repeater
- **WHEN** an operator ticks "Collect metrics" on a repeater row
- **THEN** the repeater is selected, the checkbox stays ticked after a reload, and it is polled in the next cycle if heard within the recency window

#### Scenario: A companion row
- **WHEN** the contact table shows a companion
- **THEN** that row has no "Collect metrics" checkbox

#### Scenario: A selected repeater not heard recently
- **WHEN** a selected repeater was last heard outside the recency window
- **THEN** its row states that it is skipped until it is heard again within the window

#### Scenario: Last poll shown
- **WHEN** a selected repeater has been polled
- **THEN** its row shows when and with what outcome, and links to its metrics page

#### Scenario: A repeater low on battery
- **WHEN** a repeater's latest recorded status reported 3.60 V
- **THEN** its row states that it is low on battery, showing 3.60 V, the estimated percentage and when that status was collected

#### Scenario: A mains-powered repeater
- **WHEN** a repeater's latest recorded status reported 0 V
- **THEN** its row carries no battery mark

#### Scenario: A failed poll after a low reading
- **WHEN** a repeater's latest poll was login unanswered and the status before it estimated 30%
- **THEN** its row still states that it is low on battery, with that earlier status's collection time

### Requirement: Each collected repeater has a metrics page
The system SHALL present, per repeater, a metrics page showing the latest recorded status — battery
voltage with its capacity estimate or that no battery is sensed, transmit queue length, noise
floor, last RSSI and SNR, packets received and sent, flood and direct counts, duplicate counts,
transmit and receive airtime, uptime, error flags and receive errors — with when it was collected;
the neighbour list from the latest poll that returned one, each neighbour attributed to a contact
only when its key prefix matches exactly one contact; and the poll history within the retention
window, newest first, with each poll's outcome. A status field the repeater did not return SHALL be
shown as absent rather than as zero. Counters in the status table SHALL be shown as the repeater
reported them, not as rates.

#### Scenario: A repeater never polled
- **WHEN** the metrics page of a selected repeater that has not been polled yet is opened
- **THEN** it states that no poll has been recorded, and when the next cycle is due

#### Scenario: Latest poll failed
- **WHEN** the latest poll was login unanswered and an earlier one succeeded
- **THEN** the page shows the earlier status with its collection time, and the history shows the failed poll above it

#### Scenario: Neighbours resolved
- **WHEN** a neighbour's key prefix matches exactly one known contact
- **THEN** that neighbour is shown by the contact's name, with its signal-to-noise ratio and how long before the poll it was heard

#### Scenario: Older firmware
- **WHEN** a repeater's status answer ends before the receive-airtime and receive-error fields
- **THEN** those fields are shown as not reported, and the other fields are shown normally

#### Scenario: Battery estimate shown
- **WHEN** the latest status reported 3.95 V
- **THEN** the battery reading shows 3.950 V with its estimated percentage

### Requirement: A repeater's metrics are charted over a chosen range
The metrics page SHALL chart, for one repeater over a range the operator chooses — the last
24 hours, 7 days, 30 days, or everything kept — its battery voltage, noise floor, last RSSI, last
SNR, packets received and sent per hour, transmit airtime as a share of elapsed time, and neighbour
count, each point placed at the time its poll started. The default range SHALL be the last 7 days.
Only polls that returned a status SHALL contribute points; polls that did not SHALL be marked on
the time axis by outcome. A field a poll did not report SHALL leave a gap rather than a zero. Rates
SHALL be derived from the change in a counter between consecutive polls that returned a status,
divided by the time between them; when uptime or the counter decreased between them — the repeater
restarted — no rate SHALL be drawn for that interval. Each chart SHALL state its latest value,
minimum and maximum over the range. The charts SHALL be drawn without scripts and without loading
anything from outside the panel. A range with no answered poll SHALL say so instead of drawing
empty charts. A chart SHALL draw no more than a bounded number of points; a range holding more
polls SHALL be reduced by averaging consecutive polls, keeping the minimum and maximum it reports
exact.

#### Scenario: Default range
- **WHEN** the metrics page of a repeater polled hourly for two weeks is opened
- **THEN** the charts cover the last 7 days

#### Scenario: Everything kept
- **WHEN** the operator chooses everything kept
- **THEN** the charts span from the oldest stored poll of that repeater to now

#### Scenario: A restart between polls
- **WHEN** a repeater's uptime is lower at one poll than at the poll before it
- **THEN** the packets-per-hour and airtime charts show a gap for that interval, not a negative or spiked value

#### Scenario: A failed poll in the range
- **WHEN** a poll in the range was login unanswered
- **THEN** no point is drawn for it and its time is marked as a failed poll

#### Scenario: Older firmware in the range
- **WHEN** polls in the range did not report receive airtime
- **THEN** that series has gaps for them and the other charts are unaffected

#### Scenario: Nothing answered in range
- **WHEN** the chosen range contains no poll that returned a status
- **THEN** the page states that no status was collected in that range, and offers the wider ranges

### Requirement: Each recorded poll can be opened
Each row of the poll history SHALL link to a page for that poll showing when it started, the
login identity, the route, its outcome and reason, every status field it returned (absent fields
shown as not reported, counters as reported, battery with its estimate) and every neighbour entry
it recorded, attributed on the same terms as on the metrics page. A poll that returned no status
SHALL say so. A poll that does not exist, has been pruned, or belongs to a different repeater SHALL
be answered as not found.

#### Scenario: Opening an earlier successful poll
- **WHEN** an operator follows the history link of a succeeded poll from three days ago
- **THEN** that poll's status and neighbours are shown as they were recorded then, not the latest ones

#### Scenario: Opening a failed poll
- **WHEN** an operator opens a poll recorded as login unanswered
- **THEN** the page shows its outcome and route, and states that it returned no status

#### Scenario: A poll of another repeater
- **WHEN** a poll's page is requested under a different repeater's key
- **THEN** the answer is not found
