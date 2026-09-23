# Spec Delta

## MODIFIED Requirements

### Requirement: The path hash size is a node-wide environment setting
The system SHALL read the path hash size from the `SIGHOP_PATH_HASH_SIZE` environment variable.
It SHALL accept exactly `1`, `2` or `3`, and SHALL use `3` when the variable is unset or empty.
The setting SHALL apply to the whole node. It SHALL NOT vary by identity, room, bot or channel.
Any other value SHALL fail startup with a configuration error naming the variable, the value
given and the accepted values. The system SHALL NOT clamp or round an invalid value into range.

#### Scenario: Unset
- **WHEN** the node starts with `SIGHOP_PATH_HASH_SIZE` unset
- **THEN** the path hash size in force is 3

#### Scenario: Set to a supported value
- **WHEN** the node starts with `SIGHOP_PATH_HASH_SIZE=2`
- **THEN** the path hash size in force is 2 for every identity the node loads

#### Scenario: Out of range
- **WHEN** the node starts with `SIGHOP_PATH_HASH_SIZE=4`
- **THEN** startup fails with a configuration error naming `SIGHOP_PATH_HASH_SIZE`, the value `4` and the accepted values 1, 2 and 3, and nothing is transmitted

#### Scenario: Not a number
- **WHEN** the node starts with `SIGHOP_PATH_HASH_SIZE=three`
- **THEN** startup fails with the same configuration error rather than falling back to the default
