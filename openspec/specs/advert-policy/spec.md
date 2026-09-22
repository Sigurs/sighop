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

### Requirement: Renaming a loaded identity changes the name its adverts carry without resetting its schedule
The system SHALL apply a rename of a stored identity to the running process that loaded it,
without a restart, so that every advert built after the rename carries the new name and no advert
carries a name the store no longer holds.

A rename SHALL NOT reset, advance or re-jitter that identity's flood schedule: its next scheduled
flood, the adverts it has already sent in this run, any active override and the inter-entity gap
SHALL all be exactly what they were before the rename. A rename SHALL NOT itself transmit
anything.

A rename SHALL NOT change the identity's public key or node hash, so it SHALL NOT be refused for a
node-hash collision and SHALL NOT create one. The system SHALL refuse a rename that would give two
identities loaded by the same run the same name, because a run addresses its loaded identities by
name.

A stored identity that this run has not loaded SHALL be renamable in the store, and the rename
SHALL have no effect on this run.

#### Scenario: The next advert carries the new name
- **WHEN** a loaded identity is renamed and an advert for it is then built
- **THEN** that advert carries the new name

#### Scenario: The schedule survives the rename
- **WHEN** a loaded identity with a scheduled flood is renamed
- **THEN** its next scheduled flood, its advert count for this run and any active override are unchanged

#### Scenario: A rename transmits nothing
- **WHEN** a loaded identity is renamed and no advert is requested
- **THEN** nothing is submitted to the radio and no airtime is charged

#### Scenario: A rename colliding with another loaded identity
- **WHEN** a rename would give a second identity loaded by this run the same name
- **THEN** the rename is refused and neither identity is changed

#### Scenario: Renaming an identity this run has not loaded
- **WHEN** a stored identity that this run did not load is renamed
- **THEN** the store holds the new name and this run's loaded identities and schedules are unchanged

#### Scenario: A requested advert after a rename
- **WHEN** an advert is requested for a loaded identity immediately after it is renamed
- **THEN** it is decided by the rules that already govern a requested advert, and the advert it submits carries the new name

### Requirement: An identity adopted mid-run joins the advert schedule on the ordinary rules

The system SHALL schedule adverts for an identity adopted while a run is active under every
interval, jitter, 24 hour floor and inter-entity gap rule that governs an identity loaded at
startup, with no rule relaxed and none applied twice. Its first flood SHALL fall one jittered
interval from adoption rather than immediately, so that adopting an identity does not itself put a
flood on the air.

Adoption SHALL NOT disturb the schedules of the identities already loaded: their next scheduled
floods, their advert counts for the run, any active override and the inter-entity gap in force
SHALL be exactly what they were before. The newly adopted identity SHALL be subject to the
inter-entity gap against the floods already sent, so that adoption cannot be used to send two
floods from one station inside the gap.

An adopted identity SHALL be distinguished in output as persistent rather than ephemeral, on the
same terms as one loaded at startup.

#### Scenario: An adopted identity does not advert on adoption

- **WHEN** an identity is adopted mid-run
- **THEN** no advert is submitted at the moment of adoption, and its first flood is scheduled one jittered interval later

#### Scenario: The other identities' schedules are untouched

- **WHEN** an identity is adopted while other identities are loaded
- **THEN** each already-loaded identity's next scheduled flood, advert count for this run and active override are unchanged

#### Scenario: The adopted identity respects the inter-entity gap

- **WHEN** an identity is adopted less than the inter-entity gap after another local identity's flood, and its own first flood falls due inside that gap
- **THEN** its flood is deferred until the gap has elapsed, and the deferral is logged

### Requirement: An identity withdrawn mid-run leaves the advert schedule immediately

The system SHALL stop originating adverts for an identity withdrawn while a run is active, from the
moment of withdrawal, and SHALL NOT submit an advert for it that was scheduled before the
withdrawal. An advert already submitted to the transmit scheduler SHALL be left to the scheduler's
own rules rather than retracted, because the station cannot unsay what it has queued; the system
SHALL NOT schedule another.

Withdrawal SHALL NOT reset, advance or re-jitter any other identity's schedule, and SHALL NOT
change the inter-entity gap in force — a flood that identity already sent still counts toward it.
A withdrawal SHALL NOT itself transmit anything.

A request for a one-shot zero-hop or flood advert naming a withdrawn identity SHALL be refused,
stating that this run does not hold that identity, and SHALL transmit nothing.

#### Scenario: The scheduled advert does not go out

- **WHEN** an identity is withdrawn and its next scheduled flood then falls due
- **THEN** no advert is submitted for it

#### Scenario: The remaining identities are unaffected

- **WHEN** one of several loaded identities is withdrawn
- **THEN** every remaining identity's next scheduled flood, advert count and override are unchanged, and the inter-entity gap still counts the withdrawn identity's last flood

#### Scenario: An advert requested for a withdrawn identity

- **WHEN** a zero-hop or flood advert is explicitly requested for an identity this run has withdrawn
- **THEN** the request is refused stating that this run does not hold that identity, and nothing is transmitted

### Requirement: A live advert-configuration change applies without resetting the schedule

The system SHALL apply a change to a loaded identity's stored advert configuration to the running
process that holds it, without a restart, so that adverts built after the change are scheduled
under the new configuration.

A configuration change SHALL NOT reset, advance or re-jitter that identity's schedule as a side
effect: its adverts already sent in this run, any active override and the inter-entity gap SHALL be
exactly what they were. Where the new configuration makes the next scheduled advert due sooner than
the configuration it replaced, the system SHALL re-derive that identity's next advert time from the
new configuration under the unchanged floor, jitter and gap rules rather than from the moment of
the change. A configuration change SHALL NOT itself transmit anything.

A configuration change SHALL be refused on the terms the configuration itself is refused on — a
faster interval than the floor still requires a time-limited override — and a refused change SHALL
leave the schedule in force untouched.

#### Scenario: A changed interval takes effect

- **WHEN** a loaded identity's flood interval is changed while a run is active
- **THEN** its next scheduled flood is derived from the new interval without a restart

#### Scenario: The change does not put an advert on the air

- **WHEN** a loaded identity's advert configuration is changed
- **THEN** nothing is transmitted at the moment of the change, and its advert count for this run is unchanged

#### Scenario: A change below the floor

- **WHEN** a loaded identity's flood interval is changed to less than the 24 hour floor with no override in force
- **THEN** the change is refused with the reason the floor gives, and the schedule in force is unchanged
