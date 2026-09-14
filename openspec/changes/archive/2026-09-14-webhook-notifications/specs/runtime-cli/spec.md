## ADDED Requirements

### Requirement: A webhook command surface manages webhooks
The system SHALL provide commands to add a webhook, list webhooks, show one, enable and disable one,
change its triggers, format and maximum hop count, replace its URL, remove it, and send it a sample
event. A URL SHALL be read from standard input and SHALL NOT be accepted as a command-line argument,
because arguments are visible in process listings and shell history. Every refusal SHALL be the one
the stored-configuration rules make, with its reason.

#### Scenario: Adding a webhook
- **WHEN** a webhook is added with a name, a format and triggers, and its URL on standard input
- **THEN** the output states it is enabled, its format, its triggers, its hop limit, and its target as scheme and host only

#### Scenario: A URL given as an argument
- **WHEN** an operator tries to pass the URL as a command-line argument
- **THEN** no such argument exists, and the help states that the URL is read from standard input

#### Scenario: Showing a webhook
- **WHEN** a webhook is shown
- **THEN** the output names its format, triggers, hop limit, enablement, target host, and its last successful and last failed delivery with the failure reason

#### Scenario: Testing a webhook
- **WHEN** a webhook is tested with a named trigger
- **THEN** one sample event is sent and the output states whether it was delivered, with the HTTP status or the failure reason

#### Scenario: Removing a webhook
- **WHEN** a webhook is removed
- **THEN** it is deleted, and a running process stops delivering to it for events raised afterwards

#### Scenario: No database configured
- **WHEN** any webhook command is run with no database configured
- **THEN** the command refuses and states that webhooks require durable storage

### Requirement: A run reports the webhooks it will deliver to
The system SHALL report at startup, before any traffic is handled, how many webhooks are enabled and
which triggers they subscribe to; SHALL state plainly when no webhooks will be sent because no
database is configured or because the run is a replay; and SHALL include webhook delivery counters
— delivered, failed and dropped — in the periodic status line whenever webhooks are active.

#### Scenario: A run with webhooks configured
- **WHEN** the runtime starts with enabled webhooks in the database
- **THEN** the startup output states the count of enabled webhooks and the triggers they cover

#### Scenario: A replay run
- **WHEN** a replay run starts with webhooks in the database
- **THEN** the startup output states that no webhooks are sent during a replay

#### Scenario: Periodic status
- **WHEN** the periodic status line is emitted during a run with webhooks active
- **THEN** it includes the delivered, failed and dropped webhook counts
