## Purpose

The committed development container: the interpreter and toolchain a checkout guarantees to
whoever opens it, the host devices, services and ports it reaches, the state it keeps across
rebuilds, and the host resources it deliberately does not reach — so that developing this
project needs a container engine and an editor, not a reconstructed host.

## ADDED Requirements

### Requirement: The checkout carries its own development container definition
The repository SHALL contain a development container definition that an editor or a container
engine can build from the checkout alone, with no host-specific value supplied by hand at build
time. Building it SHALL produce an environment in which the project's lint, type check and test
commands run.

#### Scenario: A fresh checkout on a host with only a container engine
- **WHEN** the development container is built from a fresh checkout on a host that carries no Python, no resolver and no project toolchain of its own
- **THEN** the build succeeds and the project's lint, type check and test commands all run inside it

#### Scenario: No host-specific value is required to build
- **WHEN** the definition is built on a host other than the one it was written on
- **THEN** the build completes without prompting for, or failing on, a value particular to the original host

### Requirement: The container pins the interpreter and resolver the project requires
The development container SHALL provide the Python version the project declares as its minimum
and targets for linting, and SHALL provide the same dependency resolver, at the same version, that
the release image installs with. It SHALL install the project's dependencies from the committed
lock file without updating it.

#### Scenario: The interpreter matches what the project declares
- **WHEN** the project's declared interpreter requirement is read inside the container
- **THEN** the interpreter available there satisfies it, and the lint target version matches it

#### Scenario: The lock file is respected, not rewritten
- **WHEN** the container's dependency install runs on first creation
- **THEN** it installs exactly what the lock file records, and the lock file in the workspace is unchanged afterwards

#### Scenario: A lock file that disagrees with the declared dependencies
- **WHEN** the lock file does not agree with the declared dependencies
- **THEN** the install fails and says so, rather than silently resolving new versions

### Requirement: The container carries the project's planning and agent tooling
The development container SHALL provide the OpenSpec command-line tool, the Claude Code
command-line tool, the GitHub command-line tool and git, each runnable by the container's
default user without further installation. Git submodule content that the repository references
SHALL be present in the workspace after the container is created.

#### Scenario: Planning commands work from inside
- **WHEN** an OpenSpec command is run inside the container against the workspace
- **THEN** it resolves the repository's own planning root and reports its changes and specs

#### Scenario: Referenced submodules are populated
- **WHEN** the container has finished being created for a fresh checkout
- **THEN** the directories the repository declares as submodules contain their checked-out content

### Requirement: The workspace's virtual environment is isolated from the host's
The development container SHALL keep the project's virtual environment outside the workspace
content that it shares with the host, so that installing dependencies inside the container neither
replaces nor is replaced by a virtual environment built on the host. Removing and rebuilding the
container SHALL NOT require repairing the host's checkout.

#### Scenario: The host's environment survives a container install
- **WHEN** the container installs the project's dependencies while the host's checkout already holds its own virtual environment
- **THEN** the host's virtual environment still works on the host afterwards, unmodified

#### Scenario: The container's environment survives a host install
- **WHEN** dependencies are installed on the host after the container has installed its own
- **THEN** the container's interpreter and installed packages are unaffected

### Requirement: The container reaches the host's attached radios
The development container SHALL expose the host's attached serial radio devices to the
container's default user, addressed by their stable by-identifier names rather than by an
enumeration-order name, and SHALL grant that user the numeric group that owns them on the host.
The definition SHALL allow a host to name different devices, or none, without editing a committed
file.

#### Scenario: Running the platform against a real modem from inside
- **WHEN** the platform is started inside the container against an exposed radio device
- **THEN** it opens the device and runs, exactly as the same command does on the host

#### Scenario: A host with different radios attached
- **WHEN** a host whose radios have different by-identifier names starts the container after naming them in its own local configuration
- **THEN** the container exposes that host's devices, with no change to any committed file

#### Scenario: A host with no radio attached
- **WHEN** a host with no radio attached starts the container, having said so in its own local configuration
- **THEN** the container starts, and only the commands that need a modem are unavailable

### Requirement: The web panel is reachable from the host browser
The development container SHALL forward the port the platform's web panel listens on, so that a
panel started inside the container is reachable from a browser on the host at that port.

#### Scenario: The panel served from inside the container
- **WHEN** the platform is started inside the container with its web panel enabled on the project's default port
- **THEN** a browser on the host reaches that panel on the same port

### Requirement: The database stays external to the container
The development container SHALL NOT run a database of its own, and SHALL take the database
connection details from the workspace's gitignored development environment file, as the host
workflow already does. The container SHALL be able to reach a database that is neither on the
host nor in the container.

#### Scenario: Tests against the configured database
- **WHEN** the test suite is run inside the container with the development environment file's database URL in the environment
- **THEN** the database-marked tests run against that external database rather than skipping

#### Scenario: No database configured
- **WHEN** the test suite is run inside the container with no database URL in the environment
- **THEN** the database-marked tests skip and the rest of the suite passes, as they do on the host

### Requirement: Command-line state survives a container rebuild
The development container SHALL preserve, across removing and rebuilding the container, the
credential and cache state of the tools it carries: the Claude Code login, the GitHub login and
the dependency resolver's download cache. It SHALL NOT store that state in the shared workspace.

#### Scenario: Rebuilding the container
- **WHEN** the container is removed and rebuilt from the same definition
- **THEN** the previously authenticated tools are still authenticated, and the dependency install reuses the existing cache

#### Scenario: Credentials never enter the workspace
- **WHEN** the workspace is inspected after those tools have been authenticated inside the container
- **THEN** no credential file has been written into the checkout

### Requirement: The container shares the host's context engine rather than keeping its own
The development container SHALL provide the project's context retrieval and cross-session memory
tools, backed by the **same** index and memory store the host uses — not a second copy. It SHALL
NOT run a model server of its own, using the host's instead, and SHALL NOT embed by a different
method than the host, so that one store is never written in two incompatible ways. Indexing
SHALL remain the host's job.

#### Scenario: A search inside the container
- **WHEN** a code search is run inside the container against a codebase the host has already indexed
- **THEN** it returns results from the host's existing index, with no indexing step and no second store

#### Scenario: Memory written on either side
- **WHEN** a decision is recorded in one environment and recalled in the other
- **THEN** both read the same memory store and the decision is found

#### Scenario: The workspace at a different path
- **WHEN** the container places the workspace at a path other than the host's
- **THEN** the store keyed by that path is a different, empty one — so the definition SHALL keep the paths identical

#### Scenario: The model server is unreachable
- **WHEN** the host's model server is not reachable from the container
- **THEN** the container still starts and says so, rather than failing to start or silently returning nothing

### Requirement: The container is not given the host's container engine
The development container SHALL NOT be given access to the host's container engine. The
repository SHALL state which of the project's own workflows therefore do not run inside the
container, and where they run instead.

#### Scenario: The container-engine gates of the build script
- **WHEN** the build script's image, smoke, replay or scan gate is attempted inside the container
- **THEN** it does not run there, and the repository documents that those gates run on the host

#### Scenario: The gates that do run inside
- **WHEN** the build script's lock, lint, type and test gates are run inside the container
- **THEN** they pass or fail exactly as they do on the host

### Requirement: Files the container writes stay usable on the host
The development container SHALL run as a non-root user whose numeric identity matches the host
user that owns the checkout, so that files created inside the container are owned by that host
user and need no ownership repair.

#### Scenario: A file created inside the container
- **WHEN** a file is created in the workspace from inside the container
- **THEN** the host user owns it and can edit and delete it without elevated privileges

#### Scenario: A file created on the host
- **WHEN** a file is created in the workspace on the host
- **THEN** the container's user can read and write it
