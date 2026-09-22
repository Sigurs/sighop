# Spec Delta

## ADDED Requirements

### Requirement: A bot created or enabled while a run is active is run without a restart

The system SHALL run a bot created or enabled while a run is active, without being restarted,
provided the bot is enabled, the run holds its entity, that entity is enabled, the entity serves no
room, and this build has the bot's driver. A bot started this way SHALL be indistinguishable from
one started at startup: it SHALL reach the mesh only through its context, act only on verified
adverts, spend from its rate limit, honour its mode, and read the durable state it already holds.

Where any of those conditions does not hold, the system SHALL NOT run the bot and SHALL state the
same reason it states at startup. Where the condition is later met — the entity adopted, the entity
or the bot enabled — the bot SHALL then be run without a restart.

A bot started mid-run SHALL start in the mode its configuration records, and SHALL NOT transmit
anything of itself at the moment it starts; an observing bot that is started SHALL still touch no
radio.

#### Scenario: A bot created from the command line

- **WHEN** an enabled bot is created on an enabled entity a run holds while that run is active
- **THEN** within the re-read interval that run runs it, and reports that it started running it

#### Scenario: A bot created through this run's own interface

- **WHEN** a bot is created through the web interface of the running process
- **THEN** that run runs it without waiting for the periodic re-read and without a restart

#### Scenario: A disabled bot is not started

- **WHEN** a bot is created with its enabled state off
- **THEN** it is not run, and the reason is stated exactly as it is at startup

#### Scenario: The entity arrives after the bot

- **WHEN** a bot is created on a stored entity this run does not hold, and that entity is later adopted
- **THEN** the bot is then run without a restart

#### Scenario: An observing bot started mid-run

- **WHEN** a bot configured to observe is started mid-run
- **THEN** it dispatches to its driver and touches no radio, exactly as an observing bot started at startup does

#### Scenario: Starting a bot does not disturb the others

- **WHEN** a bot is started mid-run
- **THEN** every bot already running keeps its durable state, its rate limit position and its counters

### Requirement: A bot stops being run when it stops qualifying to run

The system SHALL stop running a bot that stops qualifying while the run is active — the bot
disabled, its entity disabled or removed, or its entity taken up by a room — without being
restarted, and SHALL finish the dispatch already in flight rather than abandoning it mid-call. No
further dispatch SHALL be started for that bot, and the run SHALL keep running its other bots.

The bot's durable state SHALL be unaffected: being disabled is not a deletion, and the bot SHALL
resume with that state intact if it qualifies again. The system SHALL state that it has stopped
running the bot and why, so that a bot silent because it was disabled is distinguishable from one
silent because it decided to be.

#### Scenario: A bot disabled while running

- **WHEN** a running bot is disabled while a dispatch is in flight
- **THEN** that dispatch finishes, no further dispatch is started for it, the reason is stated, and the run keeps running its other bots

#### Scenario: The bot's entity is disabled

- **WHEN** the entity a running bot is bound to is disabled
- **THEN** the bot is stopped with that reason stated

#### Scenario: The durable state survives

- **WHEN** a bot is stopped because it was disabled and is later enabled again
- **THEN** it runs again without a restart, reading the durable state it held before, and does not repeat actions that state records
