# compose-deployment Specification

## Purpose

The reference deployment of sighop against an external database: how the container reaches the
modem without loosening host permissions, what privileges it runs without, how the database
location and secrets are delivered, how the web interface is published, and how migrations are
applied when the platform starts. The deployment is one service, the platform, and one compose
file serves every host.

## Requirements
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

### Requirement: The web interface is published on host loopback by default
The deployment SHALL publish the platform's web interface on the host's loopback address and port
8080 unless the operator's environment names a different published address or port, and SHALL
configure the interface — through the container's environment rather than its arguments — to accept
the host names that loopback publication on the published port is reached by. The operator's
environment MAY name one additional host name the interface accepts; when none is named, the accepted
host names SHALL be exactly the loopback ones.

#### Scenario: Opening the panel from the host
- **WHEN** the deployment is running with its defaults and a browser on the host opens the published loopback address
- **THEN** the sign-in form is served, and the request is not refused for its host name

#### Scenario: Reaching the panel from another machine
- **WHEN** another machine attempts to connect to the host's published web port under the defaults
- **THEN** no connection is accepted

#### Scenario: A different published port
- **WHEN** the operator's environment sets the published port to another value and a browser on the host opens the loopback address on that port
- **THEN** the sign-in form is served, and the request is not refused for its host name

#### Scenario: An additional host name
- **WHEN** the operator's environment names an additional host name and a request arrives with that host name
- **THEN** the request is not refused for its host name, and a request naming any other unlisted host is still refused

#### Scenario: A different published address
- **WHEN** the operator's environment sets the published address to a non-loopback address
- **THEN** the web port is bound on that host address instead of loopback

### Requirement: Secrets come from an environment file outside version control
The deployment SHALL take the sealing secret and the database location from variables supplied by
an environment file that is ignored by version control, SHALL refuse to start when either is unset,
SHALL NOT place either value in the compose file, and SHALL ship an example configuration that
contains no real secret.

#### Scenario: The compose file under version control
- **WHEN** the committed deployment files are inspected
- **THEN** no sealing secret, database password or database URL containing a password appears in them

#### Scenario: A secret variable that is unset
- **WHEN** the deployment is started without the database location or the sealing secret set
- **THEN** it refuses to start naming the missing variable

### Requirement: The platform applies outstanding migrations when it starts
The deployment SHALL consist of the platform service only. The platform applies outstanding
migrations as part of starting, so the deployment SHALL pass nothing to ask for it. A database ahead
of the platform's migration chain SHALL still be refused, exactly as outside a container.

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

### Requirement: The deployment uses an external database named by the environment
The deployment SHALL NOT run a database of its own. It SHALL pass the database location to the
platform from a `DATABASE_URL` variable supplied by the host's environment file, SHALL refuse to
start when that variable is unset, and SHALL attach the platform to a network with a route out of
the host so that an off-host database is reachable. The deployment SHALL NOT create a database
volume.

#### Scenario: Starting against the configured database
- **WHEN** the deployment is started with `DATABASE_URL` naming a reachable database
- **THEN** only the platform container starts, and it connects to that database

#### Scenario: The database location is unset
- **WHEN** the deployment is started without `DATABASE_URL` set
- **THEN** it refuses to start and names the missing variable, rather than starting without a database

#### Scenario: The resolved configuration
- **WHEN** the deployment's resolved configuration is inspected
- **THEN** it contains no database service, no database volume and no network marked internal

### Requirement: One deployment file serves every host
The deployment SHALL be a single compose file used unchanged on development and production hosts,
with every per-host difference — user and group ids, serial-device group, modem path, database
location, sealing secret, image reference and web publication settings — supplied by that host's
environment file. Every platform setting SHALL be delivered as an environment variable; the
deployment SHALL NOT give the container a command or arguments. The repository SHALL NOT carry an
override file for a particular environment.

#### Scenario: Starting on a new host
- **WHEN** an operator on a host with its own environment file runs the deployment's plain start command
- **THEN** the stack starts with that host's values, without naming an additional compose file

#### Scenario: The committed deployment files
- **WHEN** the repository's deployment files are listed
- **THEN** there is exactly one compose file

#### Scenario: How the platform is configured
- **WHEN** the compose file's platform service is read
- **THEN** it carries no command or argument list, and every setting the platform reads appears as an environment variable
