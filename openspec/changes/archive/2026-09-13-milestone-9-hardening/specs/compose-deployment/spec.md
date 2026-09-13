## Purpose

The reference deployment of sighop with its database: how the container reaches the modem
without loosening host permissions, what privileges it runs without, how the database is kept
off every network but the platform's, how secrets are delivered, and how migrations are applied
when the platform starts. The deployment is two services: the platform and its database.

## ADDED Requirements

### Requirement: The platform reaches the modem by stable path with a supplementary group
The deployment SHALL run the platform container as a user and group supplied by the operator's
environment, SHALL grant device access by adding the host's serial-device group as a
supplementary group rather than by running privileged or as root, and SHALL map the modem from a
stable host path into the container at one fixed device path, so that the container's device
name does not depend on host enumeration order.

#### Scenario: The container's identity
- **WHEN** the deployment is started with the operator's user and group ids and the host's serial-device group id in its environment
- **THEN** the platform runs as that user and group with the serial-device group as a supplementary group, and not as root

#### Scenario: The device path inside the container
- **WHEN** the modem is re-enumerated on the host under a different kernel name
- **THEN** the deployment's configured stable path still names it, and the platform inside the container still opens it at the same fixed device path

#### Scenario: A missing required value
- **WHEN** the deployment is started without the user id, group id, serial-device group id or modem path set
- **THEN** it refuses to start and names the missing value, rather than starting with a default

### Requirement: The platform container runs with the least privilege it needs
The deployment SHALL run the platform container with a read-only root filesystem, a temporary
filesystem only at the conventional temporary path, every Linux capability dropped, privilege
escalation disabled, and an init process that forwards signals and reaps children.

#### Scenario: The effective container configuration
- **WHEN** the deployment's resolved configuration is inspected
- **THEN** the platform container has a read-only root, all capabilities dropped, no-new-privileges set, and no privileged flag

#### Scenario: A write outside the temporary filesystem
- **WHEN** the platform attempts to write anywhere in its root filesystem other than the temporary path
- **THEN** the write fails

### Requirement: The database is reachable only by the platform
The deployment SHALL place the database on a network with no route outside the deployment, SHALL
publish no database port on the host, and SHALL persist database data in a named volume.

#### Scenario: Reaching the database from the host
- **WHEN** the deployment is running
- **THEN** no host port is bound for the database, and the database accepts connections only from containers on its internal network

#### Scenario: The data survives recreation
- **WHEN** the database container is removed and recreated
- **THEN** its data is intact

### Requirement: The web interface is published on host loopback by default
The deployment SHALL publish the platform's web interface on the host's loopback address only,
unless the operator changes the published address, and SHALL configure the interface to accept
the host names that loopback publication is reached by.

#### Scenario: Opening the panel from the host
- **WHEN** the deployment is running with its defaults and a browser on the host opens the published loopback address
- **THEN** the sign-in form is served, and the request is not refused for its host name

#### Scenario: Reaching the panel from another machine
- **WHEN** another machine attempts to connect to the host's published web port under the defaults
- **THEN** no connection is accepted

### Requirement: Secrets come from an environment file outside version control
The deployment SHALL take the sealing secret and the database password from variables supplied
by an environment file that is ignored by version control, SHALL refuse to start when either is
unset, SHALL NOT place either value in the compose file, and SHALL ship an example configuration
that contains no real secret.

#### Scenario: The compose file under version control
- **WHEN** the committed deployment files are inspected
- **THEN** no sealing secret, database password or database URL containing a password appears in them

#### Scenario: A secret variable that is unset
- **WHEN** the deployment is started without the database password or the sealing secret set
- **THEN** it refuses to start naming the missing variable

### Requirement: The platform applies outstanding migrations when it starts
The deployment SHALL consist of the platform and database services only, and SHALL start the
platform with the option that applies outstanding migrations before the schema-version check. A
database ahead of the platform's migration chain SHALL still be refused, exactly as outside a
container. Outside the deployment, a run not given that option SHALL keep refusing a database that
is behind.

#### Scenario: Starting the deployment on an empty database
- **WHEN** the deployment is started against a database with no schema
- **THEN** the platform applies every migration, emits an event naming the revision before and after, and continues starting

#### Scenario: Starting again at head
- **WHEN** the deployment is restarted against a database already at the expected revision
- **THEN** no migration is applied and the platform starts

#### Scenario: A database ahead of the image
- **WHEN** an older image is started against a database migrated by a newer one
- **THEN** the platform applies nothing and refuses to run, naming both revisions

### Requirement: The platform restarts after a failure but not after a deliberate stop
The deployment SHALL restart the platform container when it exits unexpectedly and SHALL NOT
restart it after the operator stops it, and SHALL give it a stop grace period at least as long as
the platform's own shutdown bound.

#### Scenario: The platform exits on a fatal error
- **WHEN** the platform process exits with a failure
- **THEN** the container is restarted

#### Scenario: The operator stops the deployment
- **WHEN** the operator stops the deployment
- **THEN** the platform receives a stop signal, completes its graceful stop within the grace period, and is not restarted
