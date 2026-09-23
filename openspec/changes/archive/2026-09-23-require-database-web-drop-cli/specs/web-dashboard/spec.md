# Spec Delta

## MODIFIED Requirements

### Requirement: The feed is preceded by the recent history the browser missed
The system SHALL paint the feed on connection with the most recent recorded packets, newest first
and bounded in number, before streaming live records, and SHALL mark where the recorded history
ends and the live stream begins.

#### Scenario: Opening the panel on a running platform
- **WHEN** a browser connects to the feed on a platform that has been running
- **THEN** recent packets are shown immediately rather than an empty pane that fills at the mesh's own rate

#### Scenario: No recorded history available
- **WHEN** the recorded history cannot be read
- **THEN** the feed starts empty and says that only live records are shown
