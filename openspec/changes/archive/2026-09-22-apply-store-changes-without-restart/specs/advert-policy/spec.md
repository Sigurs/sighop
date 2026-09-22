# Spec Delta

## ADDED Requirements

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
