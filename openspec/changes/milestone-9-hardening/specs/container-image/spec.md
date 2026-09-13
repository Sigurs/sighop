## Purpose

The container image sighop is distributed as: what it contains and deliberately does not, how
it runs as whatever user a deployment chooses on a read-only root filesystem, and how it states
which build of sighop it is.

## ADDED Requirements

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

### Requirement: The image runs the sighop command as its entry point
The system SHALL make the `sighop` command the image's entry point, so that the container's
arguments are the command's arguments, and SHALL run it as the container's first process so
that a stop request reaches the platform's own signal handling and produces its graceful stop.

#### Scenario: Arguments pass through
- **WHEN** the container is started with `run --device /dev/modem`
- **THEN** it runs `sighop run --device /dev/modem`

#### Scenario: A container stop
- **WHEN** the container is asked to stop
- **THEN** the platform performs the same graceful stop it performs for an interrupt on a terminal, within the container's stop grace period

### Requirement: The image can apply and report migrations without a checkout
The system SHALL include the migration chain in the image at a fixed location the application is
configured to find, so that the schema commands work from the image alone.

#### Scenario: Reporting the schema revision from the image
- **WHEN** the image runs the schema-status command against a reachable database
- **THEN** it reports the applied and expected revisions, without a source checkout mounted

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
