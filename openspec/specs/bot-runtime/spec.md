# bot-runtime Specification

## Purpose

The plugin host for automated entities: it binds a driver to an identity, feeds it the adverts and
direct messages that identity is entitled to see, gives it the only channel through which it may
transmit or persist anything, and holds the limits — rate, mode, isolation — that decide what a
driver is allowed to do with the radio.

## Requirements

### Requirement: A driver reaches the mesh only through its context
The system SHALL define a bot driver as an object with handlers for advert observations and for
received direct messages, and SHALL pass every handler a context that is the driver's only means of
sending a message, looking up a contact, or persisting state. A driver SHALL NOT be given the
transmit scheduler, the network bus, the modem, or a database session, so that no driver can
transmit outside the limits this capability enforces.

#### Scenario: A driver sends a message
- **WHEN** a driver sends a message through its context
- **THEN** the message is composed, routed and transmitted by the same path an operator-sent message uses, under the same transmit gate and airtime ceiling

#### Scenario: A driver has no other route to the radio
- **WHEN** a driver is invoked
- **THEN** nothing reachable from its context exposes the scheduler, the bus, the modem or the database directly

### Requirement: A bot is bound to exactly one entity, and that entity serves no room
The system SHALL bind each bot to exactly one stored entity and SHALL refuse to bind a second bot
to an entity that already has one, or to bind a bot to an entity that serves a room. A bot SHALL
receive advert observations for the whole mesh, and received direct messages only for its own
entity.

#### Scenario: Binding a second bot to one entity
- **WHEN** a bot is created on an entity that already has one
- **THEN** the creation is refused and says which bot holds that entity

#### Scenario: Binding a bot to a room server's entity
- **WHEN** a bot is created on an entity a room is bound to
- **THEN** the creation is refused and says that the entity already has a role

#### Scenario: A direct message to another entity
- **WHEN** a direct message addressed to a different local entity is received
- **THEN** the bot's driver is not invoked for it

### Requirement: Bots require durable storage
The system SHALL run bots only when a database is configured, and SHALL state at startup that no
bots are running when there is none, rather than running them against memory alone. This is
required because a driver's decisions depend on state restored before any traffic is processed, and
a driver that cannot distinguish a restart from a first run would repeat every action it has ever
taken.

#### Scenario: A run without a database
- **WHEN** the runtime starts with no database configured
- **THEN** no bot is run and the output states that bots require durable storage

#### Scenario: A run whose database is unreachable at startup
- **WHEN** a database is configured but cannot be reached
- **THEN** startup fails as it already does for any configured database, and no bot runs against partial state

#### Scenario: The database becomes degraded while running
- **WHEN** durable storage becomes unavailable during a run
- **THEN** driver actions that require persisting state are suppressed and reported with that reason, rather than taken against state that cannot be recorded

### Requirement: Driver work never runs on the reception path
The system SHALL dispatch driver handlers from a bounded queue owned by the bot runtime, never
inside a bus subscriber, never inside the decode stage, and never before an inbound direct message
has been acknowledged. When the queue is full the system SHALL drop the oldest pending dispatch,
count the drop, and report it, so that a slow driver degrades its own responsiveness and nothing
else.

#### Scenario: A slow driver
- **WHEN** a driver handler takes longer than the interval between receptions
- **THEN** reception, decoding, acknowledgement and every other subscriber proceed unaffected

#### Scenario: The dispatch queue overflows
- **WHEN** more dispatches arrive than the queue holds
- **THEN** the drop is counted and reported with the bot it belonged to, and the runtime keeps running

#### Scenario: Acknowledgement ordering
- **WHEN** a direct message addressed to a bot's entity is received and acknowledged
- **THEN** the acknowledgement is submitted before the driver is invoked, so a driver cannot delay or prevent it

### Requirement: A driver acts only on verified adverts
The system SHALL deliver an advert observation to a driver only for an advert whose signature has
verified, and SHALL deliver it together with what the reception itself established — at minimum
whether the contact was newly created, the hop count of the reception, and its signal quality — so
that a driver's decision to transmit can never rest on unverified content.

#### Scenario: An advert whose signature does not verify
- **WHEN** an advert fails signature verification
- **THEN** no driver is invoked for it

#### Scenario: A verified advert
- **WHEN** a verified advert is observed
- **THEN** the driver receives the contact, whether it was newly created, the reception's hop count and signal quality

### Requirement: Every outbound driver action spends from a rate limit
The system SHALL apply a per-bot rate limit, configured as a sustained rate and a burst, to every
action a driver takes that would transmit, and SHALL refuse the action when the limit is exhausted,
counting and reporting the refusal by reason. The limit SHALL be checked before anything is
composed or queued.

#### Scenario: A burst of adverts after an outage
- **WHEN** more driver-initiated transmissions are attempted than the rate limit allows
- **THEN** the excess is refused and counted, and the transmissions that were allowed are unaffected

#### Scenario: The limit is reported
- **WHEN** a run reports its bots
- **THEN** each bot's configured limit, its consumed allowance and its refusal counts by reason are reported

### Requirement: A bot's mode decides whether the radio is touched at all
The system SHALL give every bot a mode of either observe or active, SHALL default a newly created
bot to observe, and SHALL, in observe mode, run the driver's full decision path and record and
report every action it would have taken while transmitting nothing. Changing the mode SHALL be an
explicit operator action.

#### Scenario: A bot in observe mode decides to send
- **WHEN** a driver in observe mode sends a message through its context
- **THEN** the intended recipient and text are recorded and reported as would-have-sent, and nothing is queued for transmission

#### Scenario: A newly created bot
- **WHEN** a bot is created
- **THEN** its mode is observe until an operator changes it

#### Scenario: An active bot without the transmit gate
- **WHEN** a bot in active mode sends a message while transmission is not enabled
- **THEN** the existing transmit gate refuses the transmission exactly as it does for any other sender, and the refusal is reported

### Requirement: Durable state is per bot, namespaced, and survives restart
The system SHALL give each bot a durable key/value store, isolated to that bot, readable and
writable only through the driver's context, and restored before the driver receives its first
event. A write that cannot be persisted SHALL be reported and SHALL NOT be presented to the driver
as having succeeded.

#### Scenario: State across a restart
- **WHEN** a driver writes state and the runtime is restarted
- **THEN** the driver reads back the value it wrote

#### Scenario: One bot's state is not another's
- **WHEN** two bots write the same key
- **THEN** each reads back its own value

#### Scenario: A state write that cannot be persisted
- **WHEN** a state write fails
- **THEN** the failure is reported and the driver's write reports failure rather than success

### Requirement: A failing driver does not stop the runtime
The system SHALL isolate a driver handler that raises: the failure SHALL be reported with the bot
and the event that triggered it, counted, and otherwise ignored, and the runtime, the radio and
every other bot SHALL continue. A bot whose driver fails repeatedly SHALL remain identifiable in
the run's reporting as failing rather than idle.

#### Scenario: A driver raises
- **WHEN** a driver handler raises an exception
- **THEN** the failure is reported with the bot's name and the triggering event, and the runtime continues

#### Scenario: A driver that keeps failing
- **WHEN** a driver has raised on multiple events
- **THEN** its failure count is reported alongside its other counters

### Requirement: A disabled bot is loaded, reported and not run
The system SHALL run only bots that are enabled and whose entity is enabled, and SHALL report a bot
it did not run together with the reason, so that a bot that is silent because it is disabled is
distinguishable from one that is silent because it decided to be.

#### Scenario: A disabled bot
- **WHEN** the runtime starts with a disabled bot configured
- **THEN** the bot is reported as not running, with the reason, and receives no events

#### Scenario: A bot on a disabled entity
- **WHEN** a bot's entity is not enabled
- **THEN** the bot is reported as not running, with that reason
