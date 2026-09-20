# Spec Delta

## Purpose

Sets one node-wide path hash width for packets sighop originates. Repeaters append their hashes
at that width, so it decides how many bytes identify each hop on our floods and on the routes
peers learn back to us.

## ADDED Requirements

### Requirement: The path hash size is a node-wide environment setting
The system SHALL read the path hash size from the `SIGHOP_PATH_HASH_SIZE` environment variable.
It SHALL accept exactly `1`, `2` or `3`, and SHALL use `3` when the variable is unset or empty.
The setting SHALL apply to the whole node. It SHALL NOT vary by identity, room, bot or channel.
Any other value SHALL fail startup with a configuration error naming the variable, the value
given and the accepted values. The system SHALL NOT clamp or round an invalid value into range.

#### Scenario: Unset
- **WHEN** `sighop run` starts with `SIGHOP_PATH_HASH_SIZE` unset
- **THEN** the path hash size in force is 3

#### Scenario: Set to a supported value
- **WHEN** `sighop run` starts with `SIGHOP_PATH_HASH_SIZE=2`
- **THEN** the path hash size in force is 2 for every identity the run loads

#### Scenario: Out of range
- **WHEN** `sighop run` starts with `SIGHOP_PATH_HASH_SIZE=4`
- **THEN** startup fails with a configuration error naming `SIGHOP_PATH_HASH_SIZE`, the value `4` and the accepted values 1, 2 and 3, and nothing is transmitted

#### Scenario: Not a number
- **WHEN** `sighop run` starts with `SIGHOP_PATH_HASH_SIZE=three`
- **THEN** startup fails with the same configuration error rather than falling back to the default

### Requirement: Originated packets with an empty path use the configured width
Every packet the system originates with an empty path SHALL encode the configured path hash size
in its header's path-length byte, with a hop count of zero. This includes:
- flood adverts and zero-hop adverts
- channel posts
- direct messages and their acknowledgements sent by flood
- room-server PATH returns and replies sent by flood

#### Scenario: Flood advert
- **WHEN** an advert is flooded with the path hash size at 3
- **THEN** the encoded packet's path-length byte carries hash size 3 and hop count 0, and the path is empty

#### Scenario: Zero-hop advert
- **WHEN** a zero-hop advert is sent with the path hash size at 2
- **THEN** the encoded packet is DIRECT with an empty path and a path-length byte carrying hash size 2

#### Scenario: Channel post
- **WHEN** a channel post is sent with the path hash size at 3
- **THEN** the encoded GRP_TXT flood carries hash size 3 and an empty path

#### Scenario: Direct message sent by flood
- **WHEN** a direct message to a peer with no known route is sent by flood with the path hash size at 3
- **THEN** the encoded TXT_MSG flood carries hash size 3 and an empty path

#### Scenario: Previous behaviour kept on request
- **WHEN** `SIGHOP_PATH_HASH_SIZE=1` and any of the packets above is originated
- **THEN** its path-length byte carries hash size 1, byte-identical to the behaviour before this setting existed

### Requirement: Packets following a learned route keep that route's width
A packet the system addresses along a learned, non-empty path SHALL encode that path's own hash
size, whatever the configured setting is. The path bytes are fixed at the width they were
learned at.

#### Scenario: Direct message along a 1-byte learned route
- **WHEN** the path hash size is 3 and a direct message is sent along a learned 2-hop route recorded with 1-byte hashes
- **THEN** the encoded packet is DIRECT with hop count 2, hash size 1 and the learned 2-byte path
