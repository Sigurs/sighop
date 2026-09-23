# Spec Delta

## ADDED Requirements

### Requirement: The path store is shared, authoritative in memory, and durable
The system SHALL maintain one path store for the whole platform rather than one per entity, and
SHALL answer every lookup from memory so that route selection never waits on a database. The store
SHALL be bounded by a configurable maximum number of destinations with least-recently-updated
eviction. The system SHALL persist learned routes and restore them at startup, and SHALL report the
count restored.

#### Scenario: Two subscribers look up the same destination
- **WHEN** two subscribers query for the same destination
- **THEN** both receive the same learned path from the one shared store

#### Scenario: Restart with stored routes
- **WHEN** the process restarts with a database holding learned routes
- **THEN** those routes are restored before traffic arrives, a lookup for a restored destination returns its most recently confirmed path, and the count restored is reported at startup

#### Scenario: Lookup while the database is unreachable
- **WHEN** a route is looked up while the database is unreachable
- **THEN** the lookup is answered from memory at full speed and reports no error, because the memory store is the authority

## REMOVED Requirements

### Requirement: The path store is shared, authoritative in memory, and durable when a database is configured
**Reason**: Durability is no longer conditional; a database is required, so the memory-only store has
no configuration that reaches it.
**Migration**: Replaced by "The path store is shared, authoritative in memory, and durable" above.
