## ADDED Requirements

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
environment file. The repository SHALL NOT carry an override file for a particular environment.

#### Scenario: Starting on a new host
- **WHEN** an operator on a host with its own environment file runs the deployment's plain start command
- **THEN** the stack starts with that host's values, without naming an additional compose file

#### Scenario: The committed deployment files
- **WHEN** the repository's deployment files are listed
- **THEN** there is exactly one compose file

## MODIFIED Requirements

### Requirement: The web interface is published on host loopback by default
The deployment SHALL publish the platform's web interface on the host's loopback address and port
8080 unless the operator's environment names a different published address or port, and SHALL
configure the interface to accept the host names that loopback publication on the published port is
reached by. The operator's environment MAY name one additional host name the interface accepts; when
none is named, the accepted host names SHALL be exactly the loopback ones.

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
The deployment SHALL consist of the platform service only, and SHALL start the platform with the
option that applies outstanding migrations before the schema-version check. A database ahead of the
platform's migration chain SHALL still be refused, exactly as outside a container. Outside the
deployment, a run not given that option SHALL keep refusing a database that is behind.

#### Scenario: Starting the deployment on an empty database
- **WHEN** the deployment is started against a database with no schema
- **THEN** the platform applies every migration, emits an event naming the revision before and after, and continues starting

#### Scenario: Starting again at head
- **WHEN** the deployment is restarted against a database already at the expected revision
- **THEN** no migration is applied and the platform starts

#### Scenario: A database ahead of the image
- **WHEN** an older image is started against a database migrated by a newer one
- **THEN** the platform applies nothing and refuses to run, naming both revisions

## REMOVED Requirements

### Requirement: The database is reachable only by the platform
**Reason**: The deployment no longer runs a database; both development and production use an external database whose network exposure is managed outside this deployment.
**Migration**: Provision the database separately, restrict its access at that database's host, and set `DATABASE_URL` in the host's `./.env`. Data in a previous `pgdata` volume is not used; dump and restore it into the external database first if it is needed.
