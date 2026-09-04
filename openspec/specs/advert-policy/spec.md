# advert-policy Specification

## Purpose
When and how often this node advertises itself on the mesh: the interval floor, the jitter and
spacing across entities, the time-limited override that is required to advertise faster, and
the rule that adverts go through the scheduler rather than straight to the radio.
## Requirements
### Requirement: Flood advert intervals have a 24 hour floor
The system SHALL default each entity's flood advert interval to at least 24 hours and SHALL
refuse to configure a shorter interval except through the time-limited override below.

#### Scenario: Interval below the floor is configured
- **WHEN** an entity is configured with a flood advert interval below 24 hours and no override
- **THEN** the configuration is rejected with an error naming the floor, and no advert is scheduled at that interval

#### Scenario: Default configuration
- **WHEN** an entity is created with no explicit advert configuration
- **THEN** its flood advert interval is 24 hours

### Requirement: Zero-hop adverts are disabled by default
The system SHALL default every entity's zero-hop advert interval to disabled, and SHALL require
an explicit setting to enable it.

#### Scenario: Default configuration
- **WHEN** an entity is created with no explicit advert configuration
- **THEN** no zero-hop advert is ever scheduled for it

### Requirement: Adverts are jittered and spaced across entities
The system SHALL apply independent jitter of ±25% of the base interval to each entity's advert
schedule, SHALL enforce a configurable minimum gap between flood adverts from any two local
entities, defaulting to 10 minutes, and SHALL stagger the first advert of each entity across the
interval at startup rather than scheduling them together.

#### Scenario: Two entities would advert together
- **WHEN** two entities' jittered schedules fall within the minimum inter-entity gap
- **THEN** the later advert is deferred until the gap has elapsed, and the deferral is logged

#### Scenario: Startup with several entities
- **WHEN** the runtime starts with several advert-enabled entities
- **THEN** no entity adverts immediately as a consequence of startup, and their first adverts are spread across the interval

#### Scenario: Jitter is per entity
- **WHEN** two entities share the same base interval
- **THEN** their scheduled times differ by their independently drawn jitter

### Requirement: A faster interval requires a time-limited override
The system SHALL permit an advert interval below the floor only with an explicit override
carrying an expiry, defaulting to 1 hour and capped at 24 hours, after which the interval SHALL
automatically revert to the floor. There SHALL be no permanent override.

#### Scenario: Override without an expiry
- **WHEN** an override is requested with no expiry
- **THEN** the default expiry of 1 hour is applied

#### Scenario: Override beyond the maximum
- **WHEN** an override is requested with an expiry beyond 24 hours
- **THEN** the request is rejected

#### Scenario: Override expires
- **WHEN** an active override reaches its expiry
- **THEN** the interval reverts to the floor, the reversion is logged, and no further advert is scheduled at the faster interval

#### Scenario: Override is visible while active
- **WHEN** an override is active and periodic status is produced
- **THEN** the status states that an override is active, its interval and when it expires

### Requirement: Adverts are submitted as scheduled traffic, not sent directly
The system SHALL submit each due advert to the transmit scheduler in priority class 3 with a
deadline, and SHALL NOT hand an advert to the modem by any other route. An advert dropped by the
scheduler SHALL be logged as dropped and SHALL NOT be retried outside the entity's schedule.

#### Scenario: Advert falls due while the budget is exhausted
- **WHEN** an advert is submitted and the airtime budget is exhausted
- **THEN** it waits and is dropped on deadline expiry rather than displacing higher-priority traffic, and the drop is logged

#### Scenario: Advert content
- **WHEN** an advert is submitted for an entity
- **THEN** it is a signed advert carrying that entity's public key, timestamp and appdata, produced by the existing protocol layer

