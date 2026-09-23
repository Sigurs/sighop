# Spec Delta

## ADDED Requirements

### Requirement: The database is required and its absence is a startup failure
The system SHALL require a database. A database configuration that is absent SHALL be a startup
failure naming the variable that supplies it, not a mode the system runs in. The system SHALL NOT
carry a second path through the receive pipeline, the stores or the interface for a node that stores
nothing, and SHALL state the database in force at startup so an operator is never left guessing
which database their state is in.

#### Scenario: No database configured
- **WHEN** the runtime starts with no database configuration
- **THEN** it exits non-zero naming the variable that supplies the database, having opened no modem and served no interface

#### Scenario: Database configured
- **WHEN** the runtime starts with a database configured and reachable
- **THEN** it states the database in force and reports how many entities, contacts and paths were restored

### Requirement: Tests always run against a real database
The system's test suite SHALL run every test against a real PostgreSQL named by the environment. No
test SHALL be skipped for want of a database, and the suite SHALL NOT offer a subset that runs
without one. The suite SHALL NOT start a database of its own: a container engine is not available
everywhere the suite must run, and a suite that depended on one would run only on the machines that
had it.

Each run SHALL isolate itself within a namespace of its own inside the configured database, apply
the real migration chain into that namespace, and drop it when the run ends, so that concurrent runs
do not collide and no run leaves anything behind. No run SHALL require the privilege to create a
database.

#### Scenario: Suite run against a configured test database
- **WHEN** the test suite runs with a test database configured
- **THEN** it creates its own isolated namespace within that database, applies migrations there, runs every test against it, and drops it afterwards, leaving no tables behind

#### Scenario: Two runs against one database at once
- **WHEN** two test runs are started against the same configured database
- **THEN** each works in a namespace of its own and neither disturbs the other's tables

#### Scenario: Suite run with no database configured
- **WHEN** the suite runs with no test database configured in the environment
- **THEN** it stops before collecting tests and states once which variables name a database, rather than reporting any test as passed or skipped

#### Scenario: A run that ends abnormally
- **WHEN** a previous run was killed before it could drop its namespace
- **THEN** the next run removes the namespaces left behind before creating its own

## REMOVED Requirements

### Requirement: The database is optional and its absence is not an error
**Reason**: The only deployment always configures a database, and keeping the absence supported cost
a second path through every store, every page and every spec.
**Migration**: Configure `DATABASE_URL`. Replaced by "The database is required and its absence is a
startup failure" above.

### Requirement: Tests never require a database
**Reason**: The rule made every persistence test skippable, so a suite that passed proved less than
it appeared to. With the database required in production, a suite that can run without one tests a
configuration that no longer exists.
**Migration**: Replaced by "Tests always run against a real database" above. Running the suite now
requires `SIGHOP_TEST_DATABASE_URL` or `DATABASE_URL` to name a reachable PostgreSQL.
