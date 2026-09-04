# tx-scheduler Specification

## Purpose
The queue that decides what this node transmits and when: off by default, one packet in flight,
four priority classes with a reserve for the urgent ones, a rolling-hour airtime ceiling, and a
deadline on every submission.
## Requirements
### Requirement: Transmission is disabled by default
The system SHALL default to receive-only. With the transmit gate closed, packets SHALL be
accepted, queued, ordered, charged against the airtime budget, counted and logged exactly as
when transmitting, and SHALL be discarded at the final hand-off to the modem with the outcome
recorded as suppressed. Enabling transmission SHALL require an explicit operator action.

#### Scenario: Fresh run with no transmit flag
- **WHEN** the runtime starts with no explicit transmit-enable
- **THEN** the transmit gate is closed and no `Data` frame reaches the transport for the lifetime of the run

#### Scenario: Suppressed packet is fully accounted
- **WHEN** a packet is scheduled and reaches the hand-off with the gate closed
- **THEN** its airtime is charged against the budget, a packet TX wide event is emitted with a suppressed result, and the counters record it as they would a transmission

#### Scenario: Gate state is always visible
- **WHEN** periodic status is produced
- **THEN** it states whether the transmit gate is open or closed

### Requirement: Exactly one packet is in flight
The system SHALL hand at most one packet to the modem at a time and SHALL await its completion
before handing over the next.

#### Scenario: Two packets ready at once
- **WHEN** two packets are eligible and the first has been handed to the modem
- **THEN** the second is not handed over until the first completes, fails or times out

#### Scenario: Modem reports it is busy
- **WHEN** the modem rejects a hand-off as busy
- **THEN** the packet is requeued at the head of its own class, retried after a delay rather than immediately, and abandoned with a logged drop once a bounded attempt count is exhausted

### Requirement: Packets are ordered by priority class
The system SHALL maintain four priority classes — 0 acknowledgements, 1 direct replies to a live
request, 2 originated messages, 3 adverts — and SHALL always select from the lowest-numbered
non-empty eligible class, serving members within a class round-robin by originating entity.

#### Scenario: Higher class overtakes
- **WHEN** an acknowledgement is submitted while adverts are queued
- **THEN** the acknowledgement is selected next

#### Scenario: Round-robin within a class
- **WHEN** several entities have packets queued in the same class
- **THEN** selection rotates between entities rather than draining one entity's packets first

#### Scenario: Submitted while a packet is in flight
- **WHEN** a class 0 packet is submitted while a class 3 packet is already handed to the modem
- **THEN** the in-flight packet is not cancelled, and the class 0 packet is selected next

### Requirement: The airtime budget holds a rolling-hour ceiling
The system SHALL maintain a sliding window of transmissions charged over the preceding 3600
seconds and SHALL refuse to hand over any packet whose time on air would take the window total
above the configured ceiling, which SHALL default to 10% (360 seconds per hour). The ceiling
SHALL be enforced by the system independently of any modem-side duty-cycle setting.

#### Scenario: Ceiling holds under sustained overload
- **WHEN** packets are submitted continuously for a simulated 24 hours
- **THEN** no 3600-second interval contains more than the configured ceiling of transmission time

#### Scenario: Budget exhausted
- **WHEN** the window total has reached the ceiling
- **THEN** no further packet is handed over until the window drains, and the stall is reported in the status output

#### Scenario: Raised ceiling
- **WHEN** the ceiling is configured above 10%
- **THEN** the run proceeds under the configured value and a standing warning is recorded at startup and in each periodic status

### Requirement: Classes 0 and 1 draw on a reserve
The system SHALL stall classes 2 and 3 once the window reaches a configurable reserve threshold
below the ceiling, defaulting to 90% of it, while continuing to admit classes 0 and 1 up to the
full ceiling.

#### Scenario: Reserve reached
- **WHEN** the window total is between the reserve threshold and the ceiling
- **THEN** queued adverts and originated messages are not handed over, while acknowledgements and direct replies still are

#### Scenario: Ceiling reached
- **WHEN** the window total has reached the full ceiling
- **THEN** no class is handed over, including class 0

### Requirement: Airtime is charged at hand-off and refunded only on an explicit busy rejection
The system SHALL charge a packet's time on air against the window when it is handed to the modem
— including when the hand-off is suppressed by the transmit gate — and SHALL NOT refund that
charge when the transmission fails or times out. The single exception SHALL be an explicit busy
rejection, where the modem states it did not transmit: that charge SHALL be refunded exactly
once, against the matching charge, so a bounded retry sequence is not billed for airtime the
radio never occupied. A timeout SHALL keep its charge, because a timeout is not evidence that
nothing was transmitted. Packets that never reach hand-off SHALL NOT be charged.

#### Scenario: Transmission fails
- **WHEN** a handed-over packet completes with a failure result
- **THEN** its airtime remains charged against the window

#### Scenario: Transmission times out
- **WHEN** a handed-over packet's transmission resolves by timeout rather than by a modem response
- **THEN** its airtime remains charged, because we do not know whether the radio transmitted

#### Scenario: Modem rejects the hand-off as busy
- **WHEN** the modem rejects a hand-off with an explicit busy error
- **THEN** the charge made at that hand-off is refunded once, and each subsequent retry is charged and refunded on its own terms

#### Scenario: Packet dropped before hand-off
- **WHEN** a packet is dropped on deadline expiry or rejected at admission
- **THEN** no airtime is charged for it

### Requirement: Every submission carries a deadline and is dropped on expiry
The system SHALL require a deadline for each submission, SHALL drop a packet whose deadline has
passed rather than transmitting it late, and SHALL bound each class's queue depth, evicting the
oldest member of a class when its cap is reached. Every drop SHALL be logged with its reason and
queue wait.

#### Scenario: Deadline expires while queued
- **WHEN** a queued packet's deadline passes before it is selected
- **THEN** it is dropped, its handle resolves with a deadline-expired reason, and a wide event records the reason and the time it waited

#### Scenario: Queue cap reached
- **WHEN** a packet is submitted to a class whose queue is at its cap
- **THEN** the oldest packet in that class is dropped and logged, and the new packet is queued

### Requirement: Each transmission emits a packet TX wide event
The system SHALL emit one wide event per transmission attempt carrying at least the packet id,
originating entity id, name and type, priority class, queue wait, airtime, remaining budget
percentage, attempt number and result, where the result distinguishes success, failure, busy
retry, suppressed and dropped.

#### Scenario: Successful transmission
- **WHEN** a transmission completes successfully
- **THEN** a wide event is emitted carrying all the named fields with a success result

#### Scenario: Suppressed transmission
- **WHEN** a transmission is suppressed by the closed gate
- **THEN** a wide event is emitted carrying the same fields with a suppressed result, so a gated run's log is directly comparable with a transmitting one

#### Scenario: Packet id threads through
- **WHEN** a packet is submitted with an originating reception's packet id
- **THEN** that id appears on the TX event, joining the reply to the reception that caused it

