## MODIFIED Requirements

### Requirement: Chat is usable when history cannot be recorded, and says so
The system SHALL allow sending and receiving when the configured database is degraded, and SHALL
state in that case that the conversation is not being recorded from that point and will not
survive the run.

#### Scenario: No database configured
- **WHEN** a run with no database configured is asked to serve the interface
- **THEN** the run refuses to start the interface, so there is no chat surface whose history could silently go unrecorded

#### Scenario: The database degrades mid-conversation
- **WHEN** the database becomes unreachable during a conversation
- **THEN** sending and receiving continue, and the interface states that messages from this point are not being recorded

#### Scenario: The database is unreachable when a conversation is opened
- **WHEN** a conversation is opened while the database is degraded
- **THEN** the messages this run has seen are shown, sending is available, and the interface states that stored history cannot be read and new messages are not being recorded
