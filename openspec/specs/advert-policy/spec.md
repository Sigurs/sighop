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

### Requirement: Adverts may be driven by a persistent entity identity
The system SHALL accept an entity whose keypair was loaded from storage as an advert source on
the same terms as an ephemeral stub, applying every existing interval, jitter, gap and override
rule unchanged, and SHALL distinguish the two in its output so an operator can tell which
identities outlive the process.

#### Scenario: A loaded identity adverts
- **WHEN** an entity loaded from a keyfile is added as an advert source
- **THEN** its adverts are signed with the stored key, scheduled under the same 24 hour floor, jitter and inter-entity gap as any other entity, and its startup listing marks it persistent rather than ephemeral

#### Scenario: Persistent and ephemeral entities in one run
- **WHEN** a run carries both a loaded identity and an ephemeral stub
- **THEN** both appear in the startup listing, each marked with whether its key survives the process, and the inter-entity advert gap applies between them

### Requirement: A single zero-hop advert can be requested explicitly
The system SHALL support emitting one zero-hop advert for a named entity on explicit request,
independent of that entity's schedule, and SHALL leave the entity's zero-hop interval disabled
by default afterwards. The request SHALL be submitted through the transmit scheduler as ordinary
class 3 traffic and SHALL be charged against the airtime budget like any other advert.

#### Scenario: One-shot zero-hop advert
- **WHEN** a zero-hop advert is explicitly requested for an entity
- **THEN** exactly one zero-hop advert is submitted at priority class 3, charged against the budget, and no recurring zero-hop schedule is created

#### Scenario: The request does not change the flood schedule
- **WHEN** a one-shot zero-hop advert is emitted
- **THEN** the entity's next flood advert time is unchanged, and the inter-entity flood gap is unaffected

### Requirement: A single flood advert can be requested explicitly
The system SHALL support emitting one flood advert for a named entity on explicit request,
submitted through the transmit scheduler as ordinary class 3 traffic with a deadline and charged
against the airtime budget like any other advert. Because a flood advert costs the same wherever
it came from, the system SHALL treat a requested flood as that entity's flood advert: the entity's
next scheduled flood SHALL move one jittered interval from the moment of the request, and the
request SHALL count as the most recent flood advert for the inter-entity gap. The system SHALL
make the time remaining in the inter-entity gap readable, so a caller can decline to flood inside
it. A requested flood SHALL NOT create or change an override, and SHALL NOT change the entity's
configured interval.

#### Scenario: One-shot flood advert
- **WHEN** a flood advert is explicitly requested for an entity
- **THEN** exactly one flood advert is submitted at priority class 3 with a deadline, charged against the budget, and the entity's count of adverts sent increases by one

#### Scenario: The request moves the flood schedule
- **WHEN** a flood advert is explicitly requested for an entity whose next scheduled flood is some hours away
- **THEN** its next scheduled flood is one jittered interval after the request, not at the earlier time, so the mesh does not receive two floods from it in quick succession

#### Scenario: The request counts toward the inter-entity gap
- **WHEN** a flood advert is explicitly requested and another local entity's scheduled flood falls due within the gap
- **THEN** that scheduled flood is deferred until the gap has elapsed, and the deferral is logged

#### Scenario: The gap remaining is readable
- **WHEN** a flood advert from any local entity was submitted less than the gap ago
- **THEN** the time remaining until the gap elapses is available to a caller, and is zero once the gap has elapsed or when no flood has been submitted this run

#### Scenario: A dropped requested flood is not retried
- **WHEN** a requested flood advert is dropped by the scheduler on deadline expiry
- **THEN** the drop is logged and no further flood is submitted for that entity before its next scheduled time
