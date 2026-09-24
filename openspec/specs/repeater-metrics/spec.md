# repeater-metrics Specification

## Purpose
Periodically log in to operator-selected repeaters as a guest with a blank password and record
their status and neighbour lists, so the health of the repeaters around the station can be watched
over time without holding any repeater's admin password.

## Requirements

### Requirement: Collection is off until an operator enables it with a login identity
The system SHALL store station-wide collection settings: whether collection is enabled, the local
identity that logs in, the interval between cycles, the recency window in days, and the retention
window in days. Collection SHALL be disabled by default, the interval SHALL default to 60 minutes,
the recency window to 3 days, and the retention window to 30 days. Collection SHALL NOT be enabled
without a login identity, and SHALL NOT use an identity that is serving a room. When the login
identity is removed or stops being loaded, collection SHALL stop and say why rather than choose
another identity.

#### Scenario: A fresh database
- **WHEN** the collection settings are read from a database that has never stored them
- **THEN** collection is disabled, no identity is set, the interval is 60 minutes, the recency window is 3 days and the retention window is 30 days

#### Scenario: Enabling without an identity
- **WHEN** an operator tries to enable collection with no login identity chosen
- **THEN** the setting is refused with the reason and nothing is stored

#### Scenario: The login identity is removed
- **WHEN** the identity chosen for collection is removed
- **THEN** no further polls are sent, and the settings show that collection has no identity

#### Scenario: A room identity is chosen
- **WHEN** an operator chooses an identity that is currently serving a room
- **THEN** the setting is refused with the reason, because that identity's traffic belongs to the room

### Requirement: Only selected repeaters heard recently are polled
The system SHALL poll a contact only when it is a repeater, it has been selected for collection,
and its advert was last heard within the recency window. Selection SHALL be stored per public key
and SHALL survive restarts. A selected repeater outside the recency window SHALL be skipped for
that cycle without a poll record being written, and SHALL be polled again in a later cycle once it
is heard within the window.

#### Scenario: A selected repeater heard yesterday
- **WHEN** a cycle runs and a selected repeater's advert was last heard one day ago with a 3-day window
- **THEN** that repeater is polled

#### Scenario: A selected repeater silent for a week
- **WHEN** a cycle runs and a selected repeater was last heard seven days ago with a 3-day window
- **THEN** no packet is sent to it and no poll is recorded for it

#### Scenario: An unselected repeater
- **WHEN** a cycle runs and a repeater has not been selected
- **THEN** no packet is sent to it

#### Scenario: A contact that is not a repeater
- **WHEN** an attempt is made to select a companion, room server or sensor for collection
- **THEN** the selection is refused

### Requirement: Cycles run on the configured interval and never overlap
The system SHALL start a collection cycle when collection is enabled and the configured interval
has elapsed since the previous cycle started. Repeaters within a cycle SHALL be polled one at a
time. A cycle still running when the next is due SHALL finish first, and the next SHALL start
after it rather than alongside it. Changes to the settings or the selection SHALL take effect
without a restart, no later than the next cycle.

#### Scenario: Default interval
- **WHEN** collection is enabled with the default interval
- **THEN** cycles start about an hour apart

#### Scenario: Interval changed
- **WHEN** an operator changes the interval from 60 to 15 minutes
- **THEN** the next cycle is due 15 minutes after the previous one started, without a restart

#### Scenario: A long cycle
- **WHEN** a cycle takes longer than the interval
- **THEN** the following cycle starts after it finishes, and two cycles never poll at once

#### Scenario: Disabled mid-cycle
- **WHEN** collection is disabled while a cycle is running
- **THEN** no repeater that has not yet been started in that cycle is polled

### Requirement: A poll logs in as a guest with a blank password, then asks for status and neighbours
The system SHALL poll a repeater by sending, from the login identity, an anonymous login request
carrying an empty password and a timestamp greater than any it has sent that repeater, and on an
accepted login SHALL send a status request followed by neighbour requests. Neighbour requests SHALL
be paged from offset zero until the number of entries received reaches the total the repeater
reports, an answer carries no entries, or a fixed page limit is reached. The system SHALL NOT
offer or send any other password. Each request SHALL be routed directly when a route to the
repeater is known and flooded otherwise.

#### Scenario: A repeater with no guest password
- **WHEN** a selected repeater whose guest password is unset is polled
- **THEN** it is sent a login with an empty password, answers it, and is then asked for status and for its neighbours

#### Scenario: Paging neighbours
- **WHEN** a repeater reports 25 neighbours and each answer holds at most 11
- **THEN** three neighbour requests are sent, at offsets 0, 11 and 22, and all 25 entries are recorded

#### Scenario: A repeater with a guest password set
- **WHEN** a repeater whose guest password is set is polled
- **THEN** its login goes unanswered, no status or neighbour request is sent, and the poll is recorded as login unanswered

#### Scenario: A known route
- **WHEN** a route to the repeater has been learned
- **THEN** the login and requests are sent directly along it rather than flooded

### Requirement: Answers are accepted only when they match an outstanding request
The system SHALL accept a response only when it decrypts under the shared secret of the login
identity and the repeater being polled, and — for status and neighbour requests — only when its
echoed timestamp equals the timestamp of the request outstanding to that repeater. A response
bundled inside a returned path SHALL be accepted on the same terms as one sent directly. A
response that matches nothing outstanding SHALL be counted and discarded. Each step SHALL wait for
its answer for a bounded time derived from the frame's airtime and route; a step not answered in
that time SHALL end the poll. When an answer arrives by flood, the system SHALL wait for the
mesh's re-floods of it to subside before transmitting again, and SHALL then send the repeater a
direct path return carrying the route that flood took, as stock clients do. The next request SHALL
not be sent until every relay on that path return's route has had time to forward it.

#### Scenario: A late answer to an earlier poll
- **WHEN** a status response arrives echoing a timestamp that is no longer outstanding
- **THEN** it is counted as unmatched and nothing is recorded from it

#### Scenario: A flooded request answered by path return
- **WHEN** a flooded status request is answered by a returned path that carries the status response
- **THEN** the status is recorded and the returned route is learned

#### Scenario: A flooded answer
- **WHEN** a repeater answers a request by flood, because it holds no route to the station
- **THEN** nothing further is sent to it until the re-floods of that answer have had time to die down, and it is then sent a direct path return stating the route the flood took, so that later answers come direct

#### Scenario: A path return sent through a relay
- **WHEN** a path return is sent to a repeater along a route through one or more relays
- **THEN** the next request to it waits until each relay has had time to forward the path return, so that no relay is still transmitting when the request reaches it

#### Scenario: Status not answered
- **WHEN** a login is accepted but the status request is not answered in time
- **THEN** the poll is recorded as status unanswered and no neighbour request is sent

### Requirement: Collection yields to all other traffic and respects the transmit gate
Collection traffic SHALL be submitted at the lowest priority class. No collection packet SHALL be
submitted while transmission is disabled; a cycle that finds transmission disabled SHALL record
nothing and wait for the next interval. A submission the scheduler drops or suppresses SHALL end
that repeater's poll with the scheduler's reason recorded.

#### Scenario: Transmission disabled
- **WHEN** a cycle becomes due while transmission is disabled
- **THEN** nothing is transmitted, no poll is recorded, and the cycle is retried at the next interval

#### Scenario: Suppressed by the airtime ceiling
- **WHEN** a login submission is suppressed because the airtime ceiling is reached
- **THEN** that poll is recorded as not sent, with the scheduler's reason

### Requirement: Every poll is recorded with its outcome, status and neighbours
The system SHALL record, for each repeater polled, when the poll started, the login identity, the
route used, and the outcome — succeeded, not sent, login unanswered, status unanswered, or
neighbours incomplete. A succeeded or neighbours-incomplete poll SHALL carry every status field the
repeater returned. Neighbour entries SHALL be recorded as received: the key prefix, seconds since
heard as of the answer, and signal-to-noise ratio in decibels. A neighbour prefix SHALL be shown
as a known contact only when exactly one contact's public key starts with it.

#### Scenario: A complete poll
- **WHEN** login, status and every neighbour page are answered
- **THEN** one poll record with outcome succeeded, its status fields, and every neighbour entry are stored

#### Scenario: Neighbours cut short
- **WHEN** the status is answered but the second neighbour page is not
- **THEN** the poll is recorded as neighbours incomplete, with the status and the first page's entries

#### Scenario: An ambiguous neighbour prefix
- **WHEN** a neighbour prefix matches two known contacts
- **THEN** the neighbour is shown by its prefix and marked ambiguous, not attributed to either contact

### Requirement: Samples are pruned after the retention window
The system SHALL delete poll records and their neighbour entries older than the retention window,
checking at least once per cycle interval and at least once a day. Changing the retention window
SHALL apply at the next pruning.

#### Scenario: Old samples
- **WHEN** pruning runs with a 30-day retention window
- **THEN** every poll record started more than 30 days ago is deleted with its neighbour entries, and newer ones are kept

### Requirement: Collection needs the database and is inactive on a replay
The system SHALL run collection only in a live run with a database. A replay run SHALL never
transmit collection traffic, whatever the stored settings say.

#### Scenario: A replay run
- **WHEN** a replay is run against a database where collection is enabled
- **THEN** no collection packet is submitted and no poll is recorded
