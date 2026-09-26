# Spec Delta

## MODIFIED Requirements

### Requirement: Collection is off until an operator enables it with a login identity
The system SHALL store station-wide collection settings: whether collection is enabled, the local
identity that logs in, the interval between cycles, the recency window in days, and the retention
window, which is either a number of days or keep forever. Collection SHALL be disabled by default,
the interval SHALL default to 60 minutes, the recency window to 3 days, and the retention window to
30 days. Collection SHALL NOT be enabled without a login identity, and SHALL NOT use an identity
that is serving a room. When the login identity is removed or stops being loaded, collection SHALL
stop and say why rather than choose another identity.

#### Scenario: A fresh database
- **WHEN** the collection settings are read from a database that has never stored them
- **THEN** collection is disabled, no identity is set, the interval is 60 minutes, the recency window is 3 days and the retention window is 30 days

#### Scenario: Keeping samples forever
- **WHEN** the retention window is set to keep forever and the settings are read back
- **THEN** the retention window reads as keep forever, not as a number of days

#### Scenario: Enabling without an identity
- **WHEN** an operator tries to enable collection with no login identity chosen
- **THEN** the setting is refused with the reason and nothing is stored

#### Scenario: The login identity is removed
- **WHEN** the identity chosen for collection is removed
- **THEN** no further polls are sent, and the settings show that collection has no identity

#### Scenario: A room identity is chosen
- **WHEN** an operator chooses an identity that is currently serving a room
- **THEN** the setting is refused with the reason, because that identity's traffic belongs to the room

### Requirement: Samples are pruned after the retention window
The system SHALL delete poll records and their neighbour entries older than the retention window,
checking at least once per cycle interval and at least once a day. When the retention window is
keep forever, pruning SHALL delete nothing. Changing the retention window SHALL apply at the next
pruning.

#### Scenario: Old samples
- **WHEN** pruning runs with a 30-day retention window
- **THEN** every poll record started more than 30 days ago is deleted with its neighbour entries, and newer ones are kept

#### Scenario: Kept forever
- **WHEN** pruning runs with the retention window set to keep forever
- **THEN** no poll record or neighbour entry is deleted, however old

#### Scenario: Forever changed back to days
- **WHEN** the retention window is changed from keep forever to 30 days
- **THEN** the next pruning deletes every poll record started more than 30 days ago

## ADDED Requirements

### Requirement: Battery capacity is estimated from the reported voltage
The system SHALL estimate a repeater's remaining battery capacity, as a whole percentage from 0 to
100, from the battery voltage of a status answer, using a single-cell lithium-ion discharge curve:
4.20 V or more is 100%, 3.30 V or less is 0%, about 3.75 V is 50%, and values between curve points
are interpolated. A reading below 2.50 V (including zero) or above 4.40 V SHALL be treated as no
battery sensed — the node is taken to be mains powered or its reading to be meaningless — and SHALL
yield no estimate. A battery SHALL be reported as low only when an estimate exists and is below
50%.

#### Scenario: A full cell
- **WHEN** a status answer reports 4.15 V
- **THEN** the estimate is above 90% and the battery is not low

#### Scenario: A half-drained cell
- **WHEN** a status answer reports 3.60 V
- **THEN** the estimate is below 50% and the battery is low

#### Scenario: No battery sensed
- **WHEN** a status answer reports 0 V, or 5.10 V
- **THEN** no estimate is given and the battery is not reported as low
