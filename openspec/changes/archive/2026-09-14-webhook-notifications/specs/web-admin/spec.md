## ADDED Requirements

### Requirement: Webhooks are configured through the interface
The system SHALL list the configured webhooks with their name, format, triggers, hop limit, enabled
state, target shown as scheme and host only, and last successful and last failed delivery with the
failure reason; SHALL allow adding one, enabling and disabling it, changing its triggers, format and
hop limit, replacing its URL, removing it, and sending it a sample event of a chosen trigger with
the outcome shown. A stored URL SHALL NOT be rendered in full anywhere in the interface, including
in a form re-shown after a refused change. Removing a webhook SHALL be confirmed explicitly.

#### Scenario: Adding a webhook
- **WHEN** a webhook is added through the interface
- **THEN** its stored result is indistinguishable from the same webhook added through the command line

#### Scenario: A refused URL
- **WHEN** a webhook is submitted with a URL whose scheme is not `http` or `https`
- **THEN** the change is refused for the same reason the command line gives, and nothing is stored

#### Scenario: Viewing a stored webhook
- **WHEN** the webhooks page is opened
- **THEN** each target appears as scheme and host, and no URL path or query is present in the page source

#### Scenario: Testing from the interface
- **WHEN** an operator sends a sample event to a webhook
- **THEN** the page shows whether it was delivered, with the HTTP status or the failure reason

#### Scenario: No database configured
- **WHEN** the webhooks page is opened on a run with no database
- **THEN** the page states that webhooks require durable storage and offers no controls
