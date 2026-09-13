## MODIFIED Requirements

### Requirement: Startup refuses a database that is not at the expected schema version
The system SHALL compare the database's applied schema version against the version the running
code expects, and SHALL refuse to start when they differ, naming both versions and the command
that reconciles them. The system SHALL NOT apply migrations automatically as a side effect of
starting, unless the run was explicitly asked to migrate on start; a run so asked SHALL apply only
the migrations that bring a database that is behind up to the expected version, and SHALL still
refuse a database ahead of the code.

#### Scenario: Database behind the code
- **WHEN** the database's applied version is older than the version the code expects and the run was not asked to migrate on start
- **THEN** startup fails naming both versions and the command to apply the outstanding migrations

#### Scenario: Database behind the code, migrating on start
- **WHEN** the database's applied version is older than the version the code expects and the run was asked to migrate on start
- **THEN** the outstanding migrations are applied, an event names the version before and after, and startup continues

#### Scenario: Database ahead of the code
- **WHEN** the database's applied version is not one the running code knows
- **THEN** startup fails naming both versions rather than operating against an unknown schema, whether or not the run was asked to migrate on start
