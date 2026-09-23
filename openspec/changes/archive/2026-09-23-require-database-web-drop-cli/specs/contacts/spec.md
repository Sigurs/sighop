# Spec Delta

## ADDED Requirements

### Requirement: The contact store is durable
The system SHALL persist contacts, restoring them at startup so a peer heard on an earlier run is
addressable without hearing its advert again. The system SHALL state at startup how many contacts
were restored, so an operator is never left to assume durability.

#### Scenario: Restart with stored contacts
- **WHEN** the runtime is restarted with a database holding contacts
- **THEN** those contacts are available before any traffic arrives, the startup output reports how many were restored, and a peer among them resolves by name or key prefix immediately

#### Scenario: Restart with an empty store
- **WHEN** the runtime is restarted with a database holding no contacts
- **THEN** the startup output reports that none were restored, distinctly from having heard nothing yet

## REMOVED Requirements

### Requirement: The contact store is durable when a database is configured
**Reason**: Durability is no longer conditional; a database is required, so there is no memory-only
mode for contacts to fall back to.
**Migration**: Replaced by "The contact store is durable" above.
