# Spec Delta

## ADDED Requirements

### Requirement: A stored pre-shared key is never read back
The system SHALL NOT offer any way to read back a stored pre-shared key — not in the web interface,
not in output, not in an event. A key is supplied once, when a channel is added, and from then on
is only ever opened to decrypt and post. An operator who needs to give the key to someone else SHALL
keep the copy they supplied; the system SHALL say so where a pre-shared channel is added and where
stored channels are listed, rather than leave an operator to discover it when they need the key.

#### Scenario: Looking for a stored key
- **WHEN** an operator looks for a way to see a stored pre-shared channel's key
- **THEN** no surface shows it, and the chat page states that sighop cannot give a stored key back

#### Scenario: Adding a pre-shared channel
- **WHEN** a pre-shared key is added through the interface
- **THEN** the form states that the key will not be shown again and must be kept by whoever added it

## REMOVED Requirements

### Requirement: Channels require durable storage
**Reason**: The requirement existed to say what happened without a database and how channel commands
refused. A database is now required and there are no channel commands.
**Migration**: None. Channels are stored in the required database and administered from the panel's
chat page; a stored channel the node could not load is still reported at startup with its reason.
