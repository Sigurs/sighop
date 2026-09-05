## Purpose
The bounded ring buffer of recent receptions and transmissions that powers the live feed — what
it records, why it is best-effort rather than an audit trail, and how it is kept from filling a
disk on a busy mesh.

## ADDED Requirements

> Reference: DESIGN.md §6 (`packet_log` is a bounded ring buffer, aggressively pruned, "not the
> audit trail"), §8 (the live packet feed it exists to power), §9 (`packet_id` as the join key).

### Requirement: Recent receptions and transmissions are recorded
The system SHALL record, for each reception and each transmission outcome, a row carrying at
minimum the packet identifier, the direction, the time, the route and payload types, the path and
hop count, the size, the signal measurements where the event has them, the computed airtime, and
the decode or transmit outcome — so a recorded row can be correlated with the wide event that
described the same packet.

#### Scenario: A packet is received
- **WHEN** a reception is decoded and dispatched
- **THEN** a row is recorded carrying its packet identifier, inbound direction, types, path, size, signal measurements and outcome

#### Scenario: A transmission resolves
- **WHEN** a transmission completes, is dropped or expires
- **THEN** a row is recorded carrying its packet identifier, outbound direction, the originating entity, its priority class and its result

#### Scenario: A reception that failed to decode
- **WHEN** a frame arrives that cannot be decoded
- **THEN** a row is recorded carrying its raw bytes and the reason it could not be decoded, because a frame that is silently dropped is invisible forever

### Requirement: Logging a packet never delays or fails a packet
The system SHALL write packet log rows outside the path that decodes, dispatches and schedules
packets, and SHALL NOT let a slow or failing database delay a reception, a dispatch or a
transmission. A write that cannot be completed SHALL be discarded rather than queued without
bound.

#### Scenario: The database is slow
- **WHEN** the database is slower than the arrival rate of packets
- **THEN** reception and dispatch proceed at full rate and the backlog of unwritten rows stays bounded

#### Scenario: The database is unreachable
- **WHEN** the database is unreachable
- **THEN** packets are still received, decoded, dispatched and scheduled, and packet logging is reported as degraded

### Requirement: Rows discarded because logging fell behind are counted and reported
The system SHALL count every row it discarded because its buffer was full or its write failed,
and SHALL report that count in periodic status output, so that a gap in the feed is visible as a
gap rather than mistaken for a quiet mesh.

#### Scenario: The buffer overflows
- **WHEN** more rows are produced than can be written and the buffer is full
- **THEN** the oldest unwritten rows are discarded, the discard count increases, and the status line reports it

#### Scenario: No rows discarded
- **WHEN** every produced row was written
- **THEN** the status line reports a discard count of zero rather than omitting the field

### Requirement: The log is bounded and pruned
The system SHALL enforce a configurable maximum on the number of retained rows, SHALL prune
older rows periodically to stay within it, and SHALL report what it pruned. The bound SHALL hold
regardless of how long the process has been running.

#### Scenario: Retention bound exceeded
- **WHEN** the number of retained rows exceeds the configured maximum
- **THEN** the oldest rows beyond the maximum are deleted, and the count deleted is reported

#### Scenario: Pruning across a restart
- **WHEN** the runtime starts against a database whose packet log already exceeds the maximum
- **THEN** the excess is pruned without waiting for the periodic interval to elapse

#### Scenario: Pruning fails
- **WHEN** a pruning pass fails
- **THEN** the failure is reported and the next pass is attempted at the normal interval, rather than the task stopping

### Requirement: The packet log is a feed, not an audit trail
The system SHALL treat the packet log as evidence that may be incomplete: it SHALL NOT be used
to decide protocol behaviour, to detect duplicates, or as the record of anything the system must
be able to prove. Nothing SHALL depend on a row being present.

#### Scenario: Duplicate detection
- **WHEN** a duplicate reception arrives
- **THEN** duplicate detection uses the dedup cache and never consults the packet log

#### Scenario: The log is empty
- **WHEN** the packet log holds no rows
- **THEN** every other behaviour of the system is unchanged
