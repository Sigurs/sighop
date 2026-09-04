## ADDED Requirements

> Reference: DESIGN.md §4.4 (the virtual network bus), §4.2 step 5 (fan-out to every entity).

### Requirement: Receptions fan out to every subscriber
The system SHALL deliver each non-duplicate reception to every current subscriber, and SHALL NOT
filter by destination hash on their behalf — a reception whose destination hash matches several
subscribers reaches all of them, and each decides for itself.

#### Scenario: Multiple subscribers
- **WHEN** a reception is published and three subscribers are attached
- **THEN** all three receive the same record

#### Scenario: Destination hash matching several subscribers
- **WHEN** a reception's destination hash matches more than one subscriber's node hash
- **THEN** every matching subscriber receives it, so each can attempt its own verification

#### Scenario: Subscriber attaches mid-run
- **WHEN** a subscriber attaches after the run has started
- **THEN** it receives receptions published from that point onward and is not replayed earlier ones

### Requirement: A slow subscriber cannot stall the pipeline
The system SHALL give each subscriber an independently bounded queue, and SHALL NOT wait on a
subscriber when publishing. When a subscriber's queue is full the reception SHALL be dropped for
that subscriber only, with a wide event naming the subscriber, its queue depth and the dropped
reception.

#### Scenario: One subscriber stops consuming
- **WHEN** one subscriber stops consuming and its queue fills while others keep up
- **THEN** the other subscribers continue to receive every reception and the decode pipeline is not blocked

#### Scenario: Drop is recorded
- **WHEN** a reception is dropped because a subscriber's queue is full
- **THEN** a wide event names the subscriber, the queue depth and the reception, and the subscriber's drop counter increases

#### Scenario: Subscriber raises an exception
- **WHEN** a subscriber's handler raises
- **THEN** the exception is logged against that subscriber, the subscriber remains attached, and other subscribers are unaffected

### Requirement: Transmission is submitted through an awaitable handle
The system SHALL expose a submission API that accepts a packet with its priority class,
originating entity and deadline, and returns a handle the caller can await for the outcome of
the transmission.

#### Scenario: Submission resolves on completion
- **WHEN** a submitted packet is transmitted and completion is reported
- **THEN** the awaiting handle resolves with a successful outcome carrying the airtime charged and the queue wait

#### Scenario: Submission resolves on drop
- **WHEN** a submitted packet is dropped because its deadline expired or it was rejected at admission
- **THEN** the awaiting handle resolves with an unsuccessful outcome carrying the reason, rather than hanging or raising

#### Scenario: Submission resolves while transmit is disabled
- **WHEN** a packet is submitted with the transmit gate closed
- **THEN** the handle resolves with a suppressed outcome, which is distinguishable from both success and failure
