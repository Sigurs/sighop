## ADDED Requirements

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
