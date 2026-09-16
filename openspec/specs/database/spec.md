# database Specification

## Purpose
How sighop connects to its Postgres database, how the schema is versioned and applied, and what
happens when the database is absent, unreachable or out of date — so that a database fault
degrades the platform predictably instead of stalling the radio.

## Requirements

### Requirement: The database is optional and its absence is not an error
The system SHALL run its full receive path, decode, dedup, path learning, scheduler and monitor
surfaces with no database configured, holding contacts and paths in memory as it does today.
An absent database configuration SHALL NOT be an error, and the system SHALL state at startup
which of the two modes is in force so an operator is never left guessing whether state will
survive.

#### Scenario: No database configured
- **WHEN** the runtime starts with no database configuration
- **THEN** it starts normally, holds contacts and paths in memory, and states in its startup output that state will not survive the process

#### Scenario: Database configured
- **WHEN** the runtime starts with a database configured and reachable
- **THEN** it states that state is persistent and reports how many entities, contacts and paths were restored

### Requirement: A configured database that cannot be reached is a startup failure
The system SHALL treat a configured but unreachable, unauthenticated or unmigrated database as a
startup failure naming the cause, and SHALL NOT silently fall back to in-memory operation.
Falling back would present a running node whose durability an operator has already assumed.

#### Scenario: Configured database refuses the connection
- **WHEN** a database is configured and the connection fails
- **THEN** startup fails with an error naming the host, the database and the failure reported by the server, and nothing is transmitted

#### Scenario: Credentials are rejected
- **WHEN** the configured credentials are rejected
- **THEN** startup fails naming the role, and the failure output does not contain the password

### Requirement: Migrations are the only authority on schema
The system SHALL define its schema exclusively through ordered, reviewable migrations, SHALL
NOT create or alter tables from application code at runtime, and SHALL record the applied schema
version in the database itself.

#### Scenario: First run against an empty database
- **WHEN** migrations are applied to a database with no sighop tables
- **THEN** the tables are created and the applied version is recorded

#### Scenario: Application start does not create tables
- **WHEN** the runtime starts against a database missing its tables
- **THEN** no table is created implicitly and startup fails asking for migrations to be applied

### Requirement: Startup refuses a database that is not at the expected schema version
The system SHALL compare the database's applied schema version against the version the running
code expects, and SHALL refuse to start when they differ, naming both versions and the command
that reconciles them. The system SHALL NOT apply migrations automatically as a side effect of
starting, unless the run was explicitly asked to migrate on start; a run so asked SHALL apply only
the migrations that bring a database that is behind up to the expected version, and SHALL still
refuse a database ahead of the code.

#### Scenario: Database behind the code
- **WHEN** the database's applied version is older than the version the code expects and the run was not asked to migrate on start
- **THEN** startup fails naming both versions and the command to apply the outstanding migrations

#### Scenario: Database behind the code, migrating on start
- **WHEN** the database's applied version is older than the version the code expects and the run was asked to migrate on start
- **THEN** the outstanding migrations are applied, an event names the version before and after, and startup continues

#### Scenario: Database ahead of the code
- **WHEN** the database's applied version is not one the running code knows
- **THEN** startup fails naming both versions rather than operating against an unknown schema, whether or not the run was asked to migrate on start

### Requirement: Connections are pooled within the role's connection limit
The system SHALL bound the number of database connections one instance opens, SHALL default that
bound well below the connection limit granted to its role, and SHALL make the bound configurable.
Exhausting the pool SHALL surface as a reported wait or failure, never as an unbounded queue.

#### Scenario: Concurrent database work
- **WHEN** more concurrent database operations are requested than the pool holds
- **THEN** the excess waits for a connection with a bounded timeout, and a timeout is reported as a database error naming the operation

#### Scenario: Server rejects a connection for exceeding the role limit
- **WHEN** the server refuses a connection because the role's connection limit is reached
- **THEN** the failure is reported distinctly from an unreachable server, because the remedy is different

### Requirement: Every database operation is bounded in time
The system SHALL bound how long any database operation may take — establishing a connection,
acquiring one from the pool, and executing a statement — with configurable limits, and SHALL treat
exceeding any of them as a reported database error rather than a wait that continues. No database
operation SHALL be able to delay decoding, dispatching or scheduling a packet.

#### Scenario: The database host accepts no connection and refuses none
- **WHEN** the database host stops responding without refusing the connection, so that the attempt would otherwise wait for the driver's own default
- **THEN** the connection attempt fails within the configured bound, is reported as a database error, and persistence is marked degraded

#### Scenario: A statement runs longer than its bound
- **WHEN** a statement exceeds the configured statement bound
- **THEN** it is abandoned, reported as a database error naming the operation, and the caller continues

#### Scenario: The reception path while the database is unresponsive
- **WHEN** packets are received and scheduled while the database is unresponsive
- **THEN** the time taken to decode, dispatch and schedule each packet is unaffected

### Requirement: Timestamps are stored with time zones and handled in UTC
The system SHALL store every timestamp as a time-zone-aware value, SHALL treat UTC as the only
representation in application code, and SHALL NOT rely on the database server's configured time
zone for correctness.

#### Scenario: Round-trip through a server whose time zone is not UTC
- **WHEN** a timestamp is written and read back on a server whose configured time zone is not UTC
- **THEN** the value read back equals the value written, as an instant, and carries an explicit time zone

#### Scenario: Naive timestamp offered for storage
- **WHEN** a timestamp with no time zone is offered for storage
- **THEN** it is rejected rather than assumed to be in any particular zone

### Requirement: A database fault does not stop the radio
The system SHALL confine a database failure occurring after startup to the operation that
provoked it, SHALL report it as an error event naming the operation, and SHALL continue to
receive, decode and schedule using in-memory state. The system SHALL NOT abandon receptions,
stall the scheduler or exit because a write failed. While persistence is degraded, the system
SHALL probe for the database's return at a bounded interval rather than waiting for the next
write to discover it.

#### Scenario: The database becomes unreachable mid-run
- **WHEN** the database becomes unreachable while the runtime is running
- **THEN** reception, decode, dedup, path learning and scheduling continue, each failed write is reported, and the status line marks persistence as degraded

#### Scenario: The database returns
- **WHEN** a database that failed mid-run becomes reachable again
- **THEN** writes resume without a restart, and the status line stops marking persistence as degraded

#### Scenario: The database returns while nothing is being written
- **WHEN** a database that failed mid-run becomes reachable again during a period in which no write is attempted
- **THEN** the return is detected within a bounded interval by the system's own probe rather than by the next write, so the degraded state does not outlast the fault

### Requirement: A write taken from a buffer is never lost without being counted
The system SHALL account for every buffered write from the moment it is taken off the buffer to
be written: it SHALL be counted as written, counted as failed, or returned to the buffer still
pending. A write in progress when the writing is interrupted — by shutdown or by any other
cancellation — SHALL NOT disappear from every count at once, and the platform's discarded-write
totals SHALL NOT report zero for a period in which rows were in fact lost.

#### Scenario: Writing is interrupted while a batch is in flight
- **WHEN** the writing of a batch is cancelled after that batch was taken off the buffer and before its outcome is known
- **THEN** the batch is either returned to the buffer for the remaining shutdown to write, or counted among the writes that did not land — never silently dropped from both

#### Scenario: A shutdown that lost nothing
- **WHEN** a run stops with a reachable database and every buffered write lands
- **THEN** the discarded-write totals stay at zero and no loss is reported

### Requirement: Stopping drains what is buffered, under a bound
The system SHALL, when stopping, ask each writer to finish what it is writing and to write what it
has already buffered, rather than cancelling it where it stands. The drain SHALL be bounded by a
shutdown budget so that an unreachable or slow database cannot hold a stop open indefinitely, and
the budget SHALL apply across the writers together rather than to each in turn, so that the time a
stop may take does not grow with the number of write lanes. The budget SHALL be small enough that a
stop completes within the deployment's configured stop grace period alongside the rest of shutdown.

#### Scenario: Rows are buffered when the stop begins
- **WHEN** a run is stopped while rows remain buffered and the database is reachable
- **THEN** every buffered row is written before the stop completes, and none is lost

#### Scenario: More rows are buffered than one batch holds
- **WHEN** a run is stopped while more rows are buffered than a single batch carries
- **THEN** the stop keeps writing batches until the buffer is empty or the budget expires, rather than writing one batch and abandoning the rest

#### Scenario: The database does not answer during the stop
- **WHEN** a run is stopped while the database is unreachable and rows remain buffered
- **THEN** the stop completes within its budget rather than waiting on the database indefinitely

#### Scenario: Several lanes have rows to drain
- **WHEN** a run is stopped with rows buffered in more than one write lane
- **THEN** the lanes drain within one shared budget, and the stop does not take the budget once per lane

### Requirement: What a stop could not write is counted and reported
The system SHALL count rows still unwritten when the shutdown budget expires among the writes that
did not land, so that the discarded-write totals remain truthful, and SHALL report the loss as an
error event naming the write lane and the number of rows. A stop that wrote everything it had
SHALL report no such loss.

#### Scenario: The budget expires with rows still buffered
- **WHEN** the shutdown budget expires before every buffered row is written
- **THEN** the remaining rows are counted among the writes that did not land, and the loss is reported with the lane it belonged to and the row count

#### Scenario: The stop drains cleanly
- **WHEN** a stop writes everything that was buffered
- **THEN** no loss is reported and the counts are unchanged beyond the rows written

### Requirement: Tests never require a database
The system's test suite SHALL pass with no database available. Tests that exercise persistence
SHALL be additive, SHALL skip when no test database is configured, and SHALL isolate themselves
within the configured database without requiring the privilege to create a database.

#### Scenario: Suite run with no database configured
- **WHEN** the test suite runs with no test database configured
- **THEN** every non-database test passes and the database tests report as skipped

#### Scenario: Suite run against a configured test database
- **WHEN** the test suite runs with a test database configured
- **THEN** the database tests create their own isolated namespace within it, apply migrations there, and remove it afterwards, leaving no tables behind
