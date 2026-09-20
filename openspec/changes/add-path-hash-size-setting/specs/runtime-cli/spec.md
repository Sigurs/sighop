# Spec Delta

## ADDED Requirements

### Requirement: A run reports the path hash size in force
The startup output of `sighop run` SHALL state the path hash size that originated packets will
use, and whether it came from `SIGHOP_PATH_HASH_SIZE` or is the default. The output SHALL do this
whether or not transmission is enabled, so an operator can check the setting before keying the
transmitter.

#### Scenario: Default
- **WHEN** `sighop run` starts with `SIGHOP_PATH_HASH_SIZE` unset
- **THEN** the startup output states a path hash size of 3 bytes and marks it as the default

#### Scenario: Set from the environment
- **WHEN** `sighop run` starts with `SIGHOP_PATH_HASH_SIZE=1`
- **THEN** the startup output states a path hash size of 1 byte and names `SIGHOP_PATH_HASH_SIZE` as its source
