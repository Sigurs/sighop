# Spec Delta

## MODIFIED Requirements

### Requirement: The smoke run proves the image starts under deployment constraints
The build script SHALL start the built image as a numeric user and group that exist nowhere in the
image, with a read-only root filesystem and all capabilities dropped, and SHALL prove two things
there, failing the build if either fails. First, that the whole application imports under those
constraints — the web application, the runtime and their dependencies — so an import-time failure is
caught by the build rather than by a deployment. Second, that the entry point started with an
incomplete environment refuses to run: it exits non-zero naming what is missing, rather than starting
in a state it cannot serve from.

#### Scenario: An image that needs a writable root
- **WHEN** the built image cannot import the application on a read-only root as an arbitrary user
- **THEN** the smoke gate fails and the scan is not run

#### Scenario: The refusal path
- **WHEN** the built image's entry point is started under those constraints with the sealing secret absent
- **THEN** it exits non-zero naming the variable and printing the command that generates a value, and the gate passes on that outcome rather than on a node that started

### Requirement: The platform behaves identically inside the image and on the build host
The build script SHALL replay every committed capture both on the build host and inside the built
image, through the replay entry point, run under the same constraints as the smoke run with no
network and no database, and SHALL fail when the rendered output of any capture differs, naming the
capture. The test suite runs on the build host's C library and the image may ship a different one;
this is where the platform's own behaviour is checked on the library it ships with.

#### Scenario: A capture renders differently in the image
- **WHEN** a replayed capture's output inside the image differs from its output on the host
- **THEN** the replay gate fails naming the capture and showing the difference, and the scan is not run

#### Scenario: The gate needs no database
- **WHEN** the replay gate runs
- **THEN** neither the host replay nor the image replay opens a database, because the replay entry point requires none
