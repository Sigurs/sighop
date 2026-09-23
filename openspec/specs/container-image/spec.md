# container-image Specification

## Purpose

The container image sighop is distributed as: what it contains and deliberately does not, how
it runs as whatever user a deployment chooses on a read-only root filesystem, and how it states
which build of sighop it is.

## Requirements
### Requirement: The image is built from the locked dependency set and carries no build tooling
The system SHALL build its image from the committed dependency lock file without resolving
dependencies afresh, SHALL exclude development-only dependencies, and SHALL produce a final image
that is the pinned base image plus only the application's installed environment and the files the
application reads at run time. The final image SHALL NOT add a compiler, a package manager, the
source checkout, tests, captures, keyfiles or environment files. It SHALL NOT delete files the base
image ships: a deletion in a later layer removes nothing from what is distributed and would hide
those files from the vulnerability scan, so the base's own package managers remain, visible to the
scan, and unable to install anything under the deployment's read-only root and non-root user.

#### Scenario: A lock file that disagrees with the project
- **WHEN** the image is built while the dependency lock file is out of date with the project's declared dependencies
- **THEN** the build fails rather than resolving a different set

#### Scenario: Nothing from the working tree leaks in
- **WHEN** the built image's filesystem is inspected
- **THEN** it contains no test suite, no capture files, no keyfiles, no environment files and no version-control metadata

#### Scenario: No build toolchain added to the final image
- **WHEN** the built image's filesystem is compared with its base image
- **THEN** it adds no compiler, no package installer and no build tool such as `uv`

#### Scenario: Nothing the base ships is hidden
- **WHEN** the final stage of the image build is inspected
- **THEN** it copies files in and runs no command that deletes anything

### Requirement: The image runs as an arbitrary user on a read-only root filesystem
The system SHALL NOT hardcode a user or group in the image, SHALL make every application file
readable by any user, SHALL NOT require any path inside the image to be writable, and SHALL NOT
assume a home directory. Everything the application would otherwise write at start-up, such as
compiled bytecode, SHALL be produced at build time.

#### Scenario: Started as a user the image has never heard of
- **WHEN** the image is started with a numeric user and group that exist nowhere in it, a read-only root filesystem, and a temporary filesystem at the conventional temporary path only
- **THEN** the command-line entry point loads every application module, including the web interface, and answers its help command without a permission error

#### Scenario: No user in the image metadata
- **WHEN** the image's configuration is inspected
- **THEN** no user is set

### Requirement: The image runs the node as its entry point and takes no arguments
The system SHALL make the `sighop` entry point the image's entry point and SHALL give it no default
arguments, so that starting the container starts the node and nothing else. The image SHALL run it as
the container's first process so that a stop request reaches the platform's own signal handling and
produces its graceful stop. Configuration SHALL reach the node through the container's environment;
the container's arguments SHALL NOT be a configuration surface, and an argument given to the
container SHALL be refused rather than interpreted.

#### Scenario: Starting the container
- **WHEN** the container is started with no arguments and a complete environment
- **THEN** the node boots, applies outstanding migrations, opens the modem and serves the web interface

#### Scenario: An argument is given
- **WHEN** the container is started with any argument
- **THEN** it exits non-zero stating that configuration comes from the environment, and nothing is started

#### Scenario: A container stop
- **WHEN** the container is asked to stop
- **THEN** the platform performs the same graceful stop it performs for an interrupt on a terminal, within the container's stop grace period

### Requirement: The image applies migrations without a checkout
The system SHALL include the migration chain in the image at a fixed location the application is
configured to find, so that a container started against a database behind the code applies the
outstanding migrations from the image alone, with no source checkout mounted.

#### Scenario: Migrating from the image
- **WHEN** the image is started against a reachable database behind the code
- **THEN** it applies the outstanding migrations and emits an event naming the revision before and after, without a source checkout mounted

### Requirement: The image states its version and commit
The system SHALL record, at build time, the application version and the source commit the image
was built from, both as image metadata and in the environment the application reads, so that
every structured event emitted from the container carries them. A build from a working tree with
uncommitted changes SHALL say so in the recorded commit rather than recording the clean commit.

#### Scenario: Events from a container carry the commit
- **WHEN** the application emits a structured event inside a container built from a commit
- **THEN** the event's version and commit fields are that build's, not a placeholder

#### Scenario: A build from a modified working tree
- **WHEN** the image is built with uncommitted changes present
- **THEN** the recorded commit is marked as modified

### Requirement: The image contains no secrets
The system SHALL NOT place the sealing secret, a database URL or password, a keyfile, or any
environment file into any layer of the image, including layers of intermediate build stages that
are shipped.

#### Scenario: Inspecting the image's layers and history
- **WHEN** every layer and the build history of the image are inspected
- **THEN** no sealing secret, database credential or keyfile appears in any of them
