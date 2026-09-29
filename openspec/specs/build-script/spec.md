# build-script Specification

## Purpose

The single command that turns a checkout into a verified image: the gates it runs in order,
what makes each one fail the build, and what it reports, so that an image that exists is an
image that passed every gate.

## Requirements

### Requirement: The build runs its gates in a fixed order and stops at the first failure
The build script SHALL run, in order: formatting, lint, type check, the test suite, the image
build, a smoke run of the built image, a replay of the committed captures inside the built image,
and a vulnerability scan of the built image. It SHALL stop
at the first gate that fails, SHALL exit with a failure status naming that gate, and SHALL NOT
run any later gate after a failure.

The formatting gate SHALL check the tree against the project's configured formatter without
writing to it, so that a build never modifies the checkout it was asked to verify.

#### Scenario: A formatting failure
- **WHEN** the build is run on a tree holding a file the formatter would rewrite
- **THEN** it stops at formatting, names formatting as the failing gate, exits with a failure status, no later gate runs, and the file is left exactly as it was

#### Scenario: A lint failure
- **WHEN** the build is run on a tree with a lint violation
- **THEN** it stops after lint, names lint as the failing gate, exits with a failure status, and no image is built

#### Scenario: A test failure
- **WHEN** a test fails
- **THEN** the build stops before the image build and exits with a failure status

#### Scenario: Every gate passes
- **WHEN** every gate passes
- **THEN** the build exits successfully and reports the image reference it built, its version and its commit

### Requirement: The build refuses to proceed with a stale lock file
The build script SHALL verify that the dependency lock file agrees with the project's declared
dependencies before running any gate, and SHALL fail if it does not, rather than updating it.

#### Scenario: A declared dependency not in the lock
- **WHEN** a dependency is declared that the lock file does not contain
- **THEN** the build fails before lint, naming the lock file, and the lock file is unchanged

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

### Requirement: The vulnerability scan reports serious findings without failing the build
The build script SHALL scan the built image with a scanner whose version is pinned in the
script, SHALL print every high and critical finding in the build output — whether or not a
fixed version exists — and SHALL NOT fail the build because of a finding. It SHALL fail the
scan gate when the scan itself cannot run, so that an unscanned image is never reported as a
scanned one. Suppressing a specific finding from the report SHALL require an entry in a
committed ignore file that states the reason.

#### Scenario: A fixable critical vulnerability
- **WHEN** the image contains a package with a critical vulnerability that has a fixed version
- **THEN** the build output names the package, the finding and the fixed version, and the build does not fail on it

#### Scenario: A finding with no fix
- **WHEN** the image contains a high vulnerability with no fixed version available
- **THEN** it is reported and the build does not fail on it

#### Scenario: The scan cannot run
- **WHEN** the scanner fails to scan the image
- **THEN** the scan gate fails and names itself

#### Scenario: A suppressed finding
- **WHEN** a finding is listed in the committed ignore file with its reason
- **THEN** the scan does not fail on it, and the report still names it as suppressed

### Requirement: The build needs no tool beyond the project's own toolchain and a container engine
The build script SHALL require only the project's Python toolchain and a container engine on the
host. The scanner SHALL run from a pinned container image rather than as a host installation or a
project dependency.

#### Scenario: A host without a scanner installed
- **WHEN** the build is run on a host that has the project toolchain and a container engine but no scanner
- **THEN** every gate, the scan included, runs

### Requirement: Continuous integration builds every change and publishes the main branch
The repository SHALL carry a CI workflow that builds the image — the build script's image gate
and no other gate — on every pull request and every push to the main branch. It SHALL NOT run
the lock, formatting, lint, type-check, test, smoke, replay or scan gates, and SHALL NOT start a
database for the job; those gates are run by the build script on the developer's host. On a push
to the main branch or a manual run, and only then, it SHALL push the built image to the container
registry tagged `<commit>-<YYYYMMDD>-<HHMMSS>` (UTC), post the image reference and digest to a
Discord notification, and delete all but the newest three versions of the image package. Each
push SHALL produce exactly one package version, so that three versions are three tags. Every
third-party action SHALL be pinned to a commit.

#### Scenario: A pull request
- **WHEN** a pull request is opened or updated
- **THEN** only the image is built, and nothing is pushed, announced or deleted

#### Scenario: A push to the main branch
- **WHEN** a commit is pushed to the main branch and the image builds
- **THEN** the image is pushed as `<commit>-<YYYYMMDD>-<HHMMSS>`, a Discord message names the tag and digest, and only the newest three versions remain in the registry

#### Scenario: No other gate runs in CI
- **WHEN** the workflow runs for any trigger
- **THEN** no lint, type-check, test, smoke, replay or scan gate runs and no database service is started, even when the tree would fail one of those gates

#### Scenario: A gate fails in CI
- **WHEN** the image gate fails
- **THEN** nothing is pushed, announced or deleted
