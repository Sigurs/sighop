# Spec Delta

## ADDED Requirements

### Requirement: A capture is replayed by a module entry point
The system SHALL provide a module entry point that reads one capture file, given as its only
positional argument, and renders the receptions it contains through the same decode path the radio
drives. It SHALL take no other arguments, SHALL require no database and SHALL open no modem, and
SHALL render identically on every platform the image and the build host use, because comparing those
two renderings is the only check that the platform behaves the same on the C library it ships with
as on the one its tests run on.

#### Scenario: Replaying a committed capture
- **WHEN** the module entry point is given the path of a committed capture
- **THEN** it renders every reception the capture holds through the ordinary decode path and exits zero, having opened no modem and no database

#### Scenario: Invoked with no path or too many
- **WHEN** the module entry point is given no capture path, or more than one
- **THEN** it exits non-zero stating that exactly one capture path is required

#### Scenario: A malformed capture line
- **WHEN** a line in the capture cannot be read
- **THEN** it is reported rather than skipped silently, as it is for any other reader of a capture file
