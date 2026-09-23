# Spec Delta

## ADDED Requirements

### Requirement: Bots run only against durable storage
The system SHALL run bots only against durable storage, which the node now always has. A driver's
decisions depend on state restored before any traffic is processed, and a driver that cannot
distinguish a restart from a first run would repeat every action it has ever taken; the system SHALL
therefore suppress driver actions that require persisting state whenever storage becomes
unavailable, rather than taking them against state it cannot record.

#### Scenario: A run whose database is unreachable at startup
- **WHEN** a database is configured but cannot be reached
- **THEN** startup fails as it does for any configured database, and no bot runs against partial state

#### Scenario: The database becomes degraded while running
- **WHEN** durable storage becomes unavailable during a run
- **THEN** driver actions that require persisting state are suppressed and reported with that reason, rather than taken against state that cannot be recorded

## REMOVED Requirements

### Requirement: Bots require durable storage
**Reason**: Its first scenario described a node started with no database, which can no longer start.
The rest is restated without that case.
**Migration**: Replaced by "Bots run only against durable storage" above.
