# runtime-cli Specification

## Purpose
The `sighop run` command that wires the modem, bus, scheduler and policies into a running
node: transmission stays off without an explicit flag, status reports the operating limits, and
rendered output never presents unverified content as verified.
## Requirements
### Requirement: The run command wires the platform together
The system SHALL provide a `sighop run` command that opens a modem source, decodes receptions,
deduplicates them, learns paths, fans out on the bus, and runs the transmit scheduler with its
advert stubs, in one process.

#### Scenario: Live run
- **WHEN** `sighop run` is invoked against a serial device
- **THEN** it performs the startup handshake and probe, then processes receptions and runs the scheduler until stopped

#### Scenario: Replay run
- **WHEN** `sighop run` is invoked with a capture file instead of a device
- **THEN** the same pipeline runs against the recorded events, so scheduler and dedup behaviour can be exercised without hardware

#### Scenario: Graceful stop
- **WHEN** the command is interrupted
- **THEN** it stops the scheduler, drains and logs its queued packets as dropped with a shutdown reason, and emits a final summary

### Requirement: Transmission requires an explicit flag
The system SHALL keep the transmit gate closed unless an explicit transmit-enable flag is given
on the command, and SHALL state the gate's state at startup and in every periodic status line.

#### Scenario: Invoked without the flag
- **WHEN** `sighop run` is invoked with no transmit-enable flag
- **THEN** the startup output states that transmit is disabled and no `Data` frame is written for the run's lifetime

#### Scenario: Invoked with the flag
- **WHEN** `sighop run` is invoked with the transmit-enable flag
- **THEN** the startup output states prominently that transmit is enabled, together with the configured duty-cycle ceiling

### Requirement: Periodic status reports the operating limits
The system SHALL print a periodic status line, at a configurable interval defaulting to 60
seconds, carrying at least: transmit gate state, duty-cycle usage over the rolling hour against
the ceiling, queue depth by priority class, packets suppressed and dropped, dedup hit rate and
cache occupancy, and count of learned paths.

#### Scenario: Status during a receive-only run
- **WHEN** the status interval elapses during a gated run
- **THEN** the line reports the gate as closed, the duty-cycle usage that would have been consumed, and the remaining fields

#### Scenario: Duty-cycle usage approaching the ceiling
- **WHEN** the rolling-hour usage passes the reserve threshold
- **THEN** the status line marks the budget state distinctly so an operator can see that classes 2 and 3 are stalled

### Requirement: Rendered output never presents unverified content as verified
The system SHALL render advert content, channel sender names and any other unauthenticated field
visually distinctly from cryptographically verified identities, in the run command's output as in
every other surface.

#### Scenario: Advert with an unverified signature
- **WHEN** a reception carries an advert whose signature does not verify
- **THEN** the rendered line marks it unverified and does not present its name in the form used for verified identities

#### Scenario: Stub entity is rendered
- **WHEN** the run's in-memory advert stubs are listed in the output
- **THEN** they are marked as ephemeral stubs, so they are not mistaken for persisted identities

