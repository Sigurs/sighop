# Spec Delta

## MODIFIED Requirements

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
