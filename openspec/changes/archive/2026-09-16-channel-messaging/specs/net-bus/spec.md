## ADDED Requirements

### Requirement: Every reception is offered to any number of observers
The system SHALL offer every decoded reception, including those deduplication drops before fan-out,
to each registered reception observer with whether it was a duplicate. Observers SHALL be offered a
reception without being awaited, and an observer that raises SHALL be reported and SHALL NOT prevent
the reception reaching other observers or subscribers.

#### Scenario: Two observers
- **WHEN** two observers are registered and a duplicate reception arrives
- **THEN** both are told about it, marked as a duplicate, and no subscriber receives it

#### Scenario: A raising observer
- **WHEN** the first of two observers raises on a reception
- **THEN** the error is reported, the second observer is still told, and the reception still fans out if it is not a duplicate
