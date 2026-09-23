# Spec Delta

## MODIFIED Requirements

### Requirement: The database stays external to the container
The development container SHALL NOT run a database of its own, and SHALL take the database
connection details from the workspace's gitignored development environment file, as the host
workflow already does. The container SHALL be able to reach a database that is neither on the
host nor in the container. The test suite inside the container SHALL run against that database and
SHALL NOT depend on a container engine being reachable from inside the container, because none is.

#### Scenario: Tests against the configured database
- **WHEN** the test suite is run inside the container with the development environment file's database URL in the environment
- **THEN** every test runs against that external database, in a namespace of its own that is dropped when the run ends

#### Scenario: No database configured
- **WHEN** the test suite is run inside the container with no database URL in the environment
- **THEN** the run stops before collecting tests and states which variables name a database, exactly as it does on the host
