# Spec Delta

## MODIFIED Requirements

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
