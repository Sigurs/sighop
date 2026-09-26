# Spec Delta

## MODIFIED Requirements

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

## ADDED Requirements

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
