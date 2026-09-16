## MODIFIED Requirements

### Requirement: A record that could not be written is counted and reported, not dropped silently
The system SHALL treat a direct message record as costly to lose, retaining it for retry rather
than discarding it freely, and SHALL count and report records it could not write. A conversation
whose records were lost SHALL be reported as incomplete rather than presented as complete. This
holds at shutdown as much as during a run: a record buffered when the run stops SHALL be written
before the stop completes, or counted and reported as lost.

#### Scenario: The database is degraded and then recovers
- **WHEN** the database is unreachable while messages are sent and received, and then becomes reachable
- **THEN** the records observed during the outage are written with their latest known state

#### Scenario: Records were discarded
- **WHEN** message records have been discarded
- **THEN** the count is reported with the platform's other discarded-write counts

#### Scenario: A gap is visible
- **WHEN** a conversation is read whose records include a known gap
- **THEN** the gap is stated rather than closed over

#### Scenario: The run stops with records buffered
- **WHEN** a run is stopped while direct message records remain buffered and the database is reachable
- **THEN** every buffered record is written before the stop completes, and the conversation read after the restart is complete

#### Scenario: The run stops with records that cannot be written
- **WHEN** a run is stopped while direct message records remain buffered and they cannot be written within the shutdown budget
- **THEN** the records are counted among the platform's discarded writes and the loss is reported, rather than the stop reporting nothing
