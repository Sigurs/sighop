# Spec Delta

## ADDED Requirements

### Requirement: Webhooks are configured only through the interface
The system SHALL list the configured webhooks with their name, format, triggers, hop limit, enabled
state, target shown as scheme and host only, and last successful and last failed delivery with the
failure reason; SHALL allow adding one, enabling and disabling it, changing its triggers, format and
hop limit, replacing its URL, removing it, and sending it a sample event of a chosen trigger with
the outcome shown. A stored URL SHALL NOT be rendered in full anywhere in the interface, including
in a form re-shown after a refused change. Removing a webhook SHALL be confirmed explicitly. The
interface is the only surface through which a webhook is configured.

#### Scenario: Adding a webhook
- **WHEN** a webhook is added through the interface
- **THEN** it is stored with the name, format, triggers, hop limit and target submitted, and the running process delivers to it without a restart

#### Scenario: A refused URL
- **WHEN** a webhook is submitted with a URL whose scheme is not `http` or `https`
- **THEN** the change is refused naming the reason, and nothing is stored

#### Scenario: Viewing a stored webhook
- **WHEN** the webhooks page is opened
- **THEN** each target appears as scheme and host, and no URL path or query is present in the page source

#### Scenario: Testing from the interface
- **WHEN** an operator sends a sample event to a webhook
- **THEN** the page shows whether it was delivered, with the HTTP status or the failure reason

### Requirement: Channels are configured only through the interface
The system SHALL administer channels from the chat page: it SHALL list the stored channels there
with their name, kind, channel hash, guessable marking and recorded message count, including a
stored channel this run could not load; SHALL allow adding a channel from a hashtag or from a
pasted pre-shared key, and re-adding the Public channel if it was removed; and SHALL allow removing
a channel through an explicit confirmation that states how many messages will be deleted. A write
to a channel SHALL return the operator to the chat page. A stored pre-shared key SHALL NOT be
rendered anywhere in the interface, including in a form re-shown after a refused addition. The
interface is the only surface through which a channel is configured.

#### Scenario: Adding a channel
- **WHEN** a channel is added through the interface
- **THEN** it is stored with the kind, key and name submitted, and the running process decrypts on it without a restart

#### Scenario: Adding a hashtag channel
- **WHEN** a hashtag channel is added through the interface
- **THEN** the result states that anyone who guesses the hashtag can read and post in it

#### Scenario: A refused pre-shared key
- **WHEN** a pasted pre-shared key is refused
- **THEN** the chat page is re-shown naming the reason and with the key field empty

#### Scenario: Viewing stored channels
- **WHEN** the chat page is opened
- **THEN** no pre-shared key, in any encoding, is present in the page source

## REMOVED Requirements

### Requirement: Webhooks are configured through the interface
**Reason**: It carried a scenario for a run with no database, which can no longer start, and defined
the interface's writes by comparison with a command line that no longer exists.
**Migration**: Replaced by "Webhooks are configured only through the interface" above.

### Requirement: Channels are configured through the interface
**Reason**: It carried a scenario for a run with no database, which can no longer start, and defined
the interface's writes by comparison with a command line that no longer exists.
**Migration**: Replaced by "Channels are configured only through the interface" above.
