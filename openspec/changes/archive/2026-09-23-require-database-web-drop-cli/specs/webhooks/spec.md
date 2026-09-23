# Spec Delta

## ADDED Requirements

### Requirement: Webhooks are never sent from a replay
The system SHALL send no webhook from a replayed capture, whose receptions carry an earlier
session's timestamps. Whether transmit is enabled SHALL NOT affect webhooks, because they are not
radio transmissions.

#### Scenario: A replay
- **WHEN** a capture containing a first sighting of a repeater is replayed
- **THEN** no HTTP request is made

#### Scenario: A receive-only run
- **WHEN** a run with transmit disabled hears a new repeater and a subscribed webhook is enabled
- **THEN** the webhook is delivered

## REMOVED Requirements

### Requirement: Webhooks require durable storage and are never sent from a replay
**Reason**: Half of it described a node with no database, which can no longer start. The replay half
still holds and is restated on its own.
**Migration**: Replaced by "Webhooks are never sent from a replay" above.
