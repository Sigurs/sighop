## ADDED Requirements

### Requirement: A stopping bot worker finishes the dispatch it is running
The system SHALL, when stopping a bot, let the dispatch already in progress run to completion
under a bound rather than cancelling it where it stands, because a dispatch may be holding the
durable state write that guards an outbound action. If the dispatch has not finished within the
shutdown budget the system SHALL then stop it, SHALL report that it was cut short naming the bot,
and SHALL NOT let one slow driver hold the stop open beyond the budget. Dispatches still queued
when the stop begins SHALL NOT be started.

#### Scenario: A bot is dispatching when the run stops
- **WHEN** a run is stopped while a driver handler is executing
- **THEN** the handler runs to completion, and any durable state write it was performing lands before the stop completes

#### Scenario: A driver that does not finish
- **WHEN** a run is stopped while a driver handler is executing and it has not finished within the shutdown budget
- **THEN** the dispatch is stopped, the fact that it was cut short is reported with the bot's name, and the stop completes

#### Scenario: Dispatches are still queued at the stop
- **WHEN** a run is stopped while dispatches remain queued behind the one in progress
- **THEN** the queued dispatches are not started, and the stop is not extended by them

#### Scenario: No bot is dispatching
- **WHEN** a run is stopped while every bot worker is idle
- **THEN** the stop completes without waiting and nothing is reported as cut short
