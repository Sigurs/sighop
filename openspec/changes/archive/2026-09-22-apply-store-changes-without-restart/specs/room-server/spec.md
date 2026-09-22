# Spec Delta

## ADDED Requirements

### Requirement: A room created while a run is active is served without a restart

The system SHALL serve a room created while a run is active, without being restarted, provided that
run holds the identity the room is bound to and that identity is enabled. A room served this way
SHALL be indistinguishable from one bound at startup: it SHALL own the packets addressed to its
identity, answer login, keep-alive, status and telemetry requests under the same rules, route
replies the way the request arrived, and be counted in periodic status output alongside the rooms
the run started with.

Where the run does not hold the room's identity — it is not loaded, or it is not enabled — the
system SHALL NOT serve the room and SHALL state the same reason it states at startup for a room it
does not serve. Where that identity is later adopted, the room SHALL then be served without a
restart, so that creating an identity and a room in either order reaches the same state.

Taking up a room SHALL NOT disturb the rooms already served, and SHALL NOT transmit anything of
itself.

#### Scenario: A room created from the command line

- **WHEN** a room is created on an identity a run holds while that run is active
- **THEN** that run serves it without a restart, and answers requests addressed to its identity

#### Scenario: A room created through this run's own interface

- **WHEN** a room is created through the web interface of the running process
- **THEN** that run serves it without waiting for the periodic re-read and without a restart

#### Scenario: A room on an identity this run does not hold

- **WHEN** a room is created on a stored identity that is not loaded by this run
- **THEN** the room is not served, and the run states that its identity was not loaded, exactly as it does at startup

#### Scenario: The identity arrives after the room

- **WHEN** a room is created on an identity this run does not hold, and that identity is later adopted by the run
- **THEN** the room is then served without a restart

#### Scenario: The rooms already served are undisturbed

- **WHEN** a new room is taken up mid-run
- **THEN** every room already served keeps its members, its unsynced positions and its counters, and no member is logged out

### Requirement: A room stops being served when its identity stops being held

The system SHALL stop serving a room whose bound identity is disabled or removed while the run is
active, without being restarted, and SHALL answer no login, keep-alive, status or telemetry request
addressed to it. The room, its members and its stored messages SHALL be unaffected: an identity
being disabled is not a deletion, and the room SHALL be served again, with its membership and
history intact, if that identity is enabled again.

The system SHALL state that it has stopped serving the room and why, because a room that has gone
silent because its identity was disabled must be distinguishable from one that has failed.

#### Scenario: The room's identity is disabled

- **WHEN** the identity a served room is bound to is disabled while the run is active
- **THEN** the run stops serving that room without a restart, answers no request addressed to it, and states the reason

#### Scenario: The room and its history survive

- **WHEN** a room stops being served because its identity was disabled
- **THEN** the room, its members and its stored messages are unchanged, and it is listed as stored but not served

#### Scenario: The identity is enabled again

- **WHEN** the identity of a room that stopped being served is enabled again
- **THEN** the run serves that room again without a restart, with its membership and message history intact
