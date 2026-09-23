# Spec Delta

## ADDED Requirements

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

## REMOVED Requirements

### Requirement: The image runs the sighop command as its entry point
**Reason**: Its contract was that the container's arguments are the command's arguments, which stops
being true when the command line is removed.
**Migration**: Replaced by "The image runs the node as its entry point and takes no arguments" above.
Deployments passing `run --device ...` supply the environment instead.

### Requirement: The image can apply and report migrations without a checkout
**Reason**: Its scenario was a schema-status command run from the image, and that command surface is
removed. Applying migrations from the image remains and is restated.
**Migration**: Replaced by "The image applies migrations without a checkout" above. The applied and
expected revisions are reported by the node's own startup event.
