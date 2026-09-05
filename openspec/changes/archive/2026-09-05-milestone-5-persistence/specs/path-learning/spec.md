## REMOVED Requirements

### Requirement: The path store is shared and in-memory
**Reason**: Only the "in-memory" half is retired. DESIGN.md §6 gives paths a table, and a node
that relearns its whole RF neighbourhood from scratch on every restart discards evidence it
already paid airtime to obtain — including zero-hop routes, which are the most useful a node can
have and are only learned when the neighbour happens to advert again.

**Migration**: Replaced by "The path store is shared, authoritative in memory, and durable when a
database is configured" below, which restates the shared-single-store rule and the bound with
least-recently-updated eviction unchanged. A run with no database behaves exactly as before,
including the empty store after a restart.

## ADDED Requirements

> Reference: DESIGN.md §4.2 (path knowledge is a property of the RF neighbourhood, shared
> platform-wide), §6 (the `path` table). What is learned, how it is keyed, and
> most-recently-confirmed-wins are unchanged by this delta and remain as specified.

### Requirement: The path store is shared, authoritative in memory, and durable when a database is configured
The system SHALL maintain one path store for the whole platform rather than one per entity, and
SHALL answer every lookup from memory so that route selection never waits on a database. The
store SHALL be bounded by a configurable maximum number of destinations with least-recently-updated
eviction. When a database is configured the system SHALL additionally persist learned routes and
restore them at startup; when none is configured the store SHALL be memory-only as before.

#### Scenario: Two subscribers look up the same destination
- **WHEN** two subscribers query for the same destination
- **THEN** both receive the same learned path from the one shared store

#### Scenario: Restart with a database configured
- **WHEN** the process restarts with a database holding learned routes
- **THEN** those routes are restored before traffic arrives, a lookup for a restored destination returns its most recently confirmed path, and the count restored is reported at startup

#### Scenario: Restart with no database configured
- **WHEN** the process restarts with no database configured
- **THEN** the path store is empty and is relearned from received traffic

#### Scenario: Lookup while the database is unreachable
- **WHEN** a route is looked up while the database is unreachable
- **THEN** the lookup is answered from memory at full speed and reports no error, because the memory store is the authority

### Requirement: Route persistence happens off the reception path
The system SHALL NOT make decoding, deduplicating or dispatching a reception wait on a route
being written, and SHALL bound the work outstanding behind that path. A route that could not be
written SHALL be discarded rather than queued without bound, and the discard SHALL be counted,
because a route is cheap to relearn from the next reception.

#### Scenario: Routes learned faster than they are written
- **WHEN** routes are learned faster than the database accepts them
- **THEN** reception and dispatch proceed at full rate, the outstanding work stays bounded, and the number of unwritten routes is counted and reported

#### Scenario: Write fails
- **WHEN** a route write fails
- **THEN** the in-memory route is unaffected and usable, and the failure is reported as an error

### Requirement: A restored route is a candidate, not a confirmation
The system SHALL restore a route with the confirmation time it had when it was learned, and
SHALL NOT treat restoration as fresh confirmation. A route confirmed by a live reception SHALL
therefore win over an older restored one under the existing most-recently-confirmed rule.

#### Scenario: A restored route and a freshly learned one
- **WHEN** a route is restored from the database and a different route to the same destination is then learned from a reception
- **THEN** the freshly learned route wins the lookup, and both remain recorded

#### Scenario: Ambiguity survives the round trip
- **WHEN** a route keyed only on a node hash, and therefore marked ambiguous, is persisted and restored
- **THEN** it is still marked ambiguous, because restoring it resolves nothing about which node the hash denoted
