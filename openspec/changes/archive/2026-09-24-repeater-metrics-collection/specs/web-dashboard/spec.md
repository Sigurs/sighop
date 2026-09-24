# Spec Delta

## ADDED Requirements

### Requirement: Repeaters are selected for collection from the contact table
The contact table SHALL show, for each repeater contact, a "Collect metrics" checkbox reflecting
whether that repeater is selected for collection, and changing it SHALL store the selection without
a page reload and without a restart. Contacts that are not repeaters SHALL have no checkbox. For
each selected repeater the table SHALL show when it was last polled and that poll's outcome, and
link to its metrics page. A selected repeater outside the recency window SHALL be marked as
skipped for that reason. Selection SHALL NOT be offered when the run has no database.

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

### Requirement: Each collected repeater has a metrics page
The system SHALL present, per repeater, a metrics page showing the latest recorded status — battery
voltage, transmit queue length, noise floor, last RSSI and SNR, packets received and sent, flood and
direct counts, duplicate counts, transmit and receive airtime, uptime, error flags and receive
errors — with when it was collected; the neighbour list from the latest poll that returned one,
each neighbour attributed to a contact only when its key prefix matches exactly one contact; and
the poll history within the retention window, newest first, with each poll's outcome. A status
field the repeater did not return SHALL be shown as absent rather than as zero. Counters SHALL be
shown as the repeater reported them, not as rates.

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
