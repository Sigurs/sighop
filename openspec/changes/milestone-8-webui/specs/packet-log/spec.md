## ADDED Requirements

### Requirement: Recent recorded packets are readable
The system SHALL make the most recently recorded packets readable, newest first, in a bounded
number per request, so that a display can show what happened before it connected. The read SHALL
be bounded in time like every other database operation, SHALL be refused rather than queued while
the database is degraded, and SHALL NOT be consulted by anything on the reception, dedup, dispatch
or transmit path.

#### Scenario: Reading the recent packets
- **WHEN** the recent recorded packets are requested with a bound
- **THEN** at most that many rows are returned, newest first

#### Scenario: The database is degraded
- **WHEN** the recent packets are requested while the database is unreachable
- **THEN** the request is answered as unavailable within the configured bound, and nothing is queued for later

#### Scenario: The read does not touch the packet path
- **WHEN** recent packets are being read
- **THEN** reception, deduplication, dispatch and transmission are unaffected, and no decision on those paths consults the log

#### Scenario: An unparsed frame is returned as recorded
- **WHEN** the recent packets include a frame that could not be decoded
- **THEN** it is returned with its raw bytes and its reason, exactly as recorded
