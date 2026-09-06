## ADDED Requirements

> Reference: milestone 7 adds a command surface for bots and run-time reporting for them, following
> the shape `sighop room` established. No existing command changes.

### Requirement: A bot command surface manages bots, their mode and their configuration
The system SHALL provide commands to create a bot on a stored identity with a named driver, to list
bots, to show one, to enable and disable one, to switch one between observe and active mode, to set
its driver configuration, and to inspect its durable state. Creating a bot SHALL name the driver
explicitly and SHALL refuse an unknown driver, listing the drivers that exist.

#### Scenario: Creating a bot
- **WHEN** a bot is created on a stored identity with a named driver
- **THEN** the bot exists, enabled, in observe mode, with the driver's default configuration, and the output states each of those including that it will transmit nothing until it is made active

#### Scenario: Creating a bot with an unknown driver
- **WHEN** a bot is created naming a driver that does not exist
- **THEN** the command refuses and lists the available drivers

#### Scenario: Showing a bot
- **WHEN** a bot is shown
- **THEN** the output names its identity, its driver, its mode, its enablement, its configuration, its limits and its counters

#### Scenario: Switching a bot to active
- **WHEN** a bot is switched to active mode
- **THEN** the output states that the bot may now transmit, and that transmission still requires the run's transmit flag

#### Scenario: Setting driver configuration
- **WHEN** a configuration value is set that the driver rejects
- **THEN** the command refuses with the driver's reason and the stored configuration is unchanged

#### Scenario: Inspecting durable state
- **WHEN** a bot's state is inspected
- **THEN** the stored keys and values are reported, and clearing state states what it will make the bot do again

#### Scenario: Deciding per contact whether it has been greeted
- **WHEN** an operator clears or sets one contact's greeting record
- **THEN** the command names the contact it resolved, and states what the bot will now do about it

### Requirement: A run runs the bots that are configured and says what it is running
The system SHALL run every enabled bot bound to an enabled entity when it starts, SHALL report each
of them before any traffic is handled — with its driver, mode and limits — SHALL report a bot it
did not run together with the reason, and SHALL state plainly when no database is configured that
no bots are running because bots require durable storage.

#### Scenario: A run with bots configured
- **WHEN** the runtime starts with bots in the database
- **THEN** each bot, its identity, driver, mode and limits are reported before the first frame is handled

#### Scenario: A run without a database
- **WHEN** the runtime starts with no database configured
- **THEN** the output states that no bots are run because bots require durable storage, rather than omitting the subject

#### Scenario: A bot that is not run
- **WHEN** a bot is disabled, or bound to an entity that is not enabled
- **THEN** it is reported as not running, with the reason

### Requirement: Bot activity is rendered as run output
The system SHALL render each bot decision as it happens — an action taken, an action that would
have been taken in observe mode, or an action suppressed with its reason — naming the bot and the
peer, and SHALL include per-bot counters in the periodic status report, so that an operator
watching a run can tell a bot that is deciding not to act from one that is not being asked to act.

#### Scenario: A driver acts
- **WHEN** a bot transmits
- **THEN** a line names the bot, the peer, and the outcome including whether it was acknowledged

#### Scenario: A driver would have acted
- **WHEN** a bot in observe mode decides to act
- **THEN** a line marks it plainly as an observation that transmitted nothing, and is not presented as a transmission

#### Scenario: Periodic status
- **WHEN** the periodic status report is emitted
- **THEN** each running bot's actions, observations, suppressions by reason, dropped dispatches and driver failures are included
