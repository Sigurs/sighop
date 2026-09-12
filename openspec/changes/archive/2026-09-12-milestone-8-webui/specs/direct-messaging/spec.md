## ADDED Requirements

### Requirement: Sent and received messages are offered to an optional durable sink
The system SHALL offer each direct message it sends and each it receives to an optional sink, on
the same contract the contact and path stores' sinks hold: the offer never awaits and never raises,
the absence of a sink changes nothing about sending, receiving, acknowledging or reporting, and no
decision on the message path depends on what the sink does with what it is offered.

#### Scenario: No sink configured
- **WHEN** messages are sent and received with no sink configured
- **THEN** every behaviour of sending, routing, retrying, acknowledging and reporting is identical to a build without the sink

#### Scenario: A sink that fails
- **WHEN** the sink cannot accept what it is offered
- **THEN** the acknowledgement, the retry schedule and the reported outcome are unchanged, and the failure does not propagate into the message path

#### Scenario: An offer is made for both directions
- **WHEN** a message is sent and a message is received
- **THEN** the sink is offered both, each carrying the local identity, the peer, the direction and the identifiers that join it to its packets

#### Scenario: A send's outcome is offered when it is known
- **WHEN** a send resolves
- **THEN** the sink is offered its final outcome for the same message it was offered at submission, identified as the same message

### Requirement: Sends may be initiated concurrently, with per-conversation ordering preserved
The system SHALL allow more than one send to be initiated by callers other than the operator's
one-shot, including while another send is in flight, and SHALL preserve the order of messages
within one identity-and-peer conversation: a message submitted after another to the same peer from
the same identity is not transmitted before it.

#### Scenario: Two sends to different peers
- **WHEN** two sends to different peers are initiated at once
- **THEN** both proceed under the platform's existing scheduling and neither waits on the other's acknowledgement window

#### Scenario: Two sends to the same peer
- **WHEN** two sends from one identity to one peer are initiated in order
- **THEN** the first is transmitted before the second, and their recorded order matches the order they were submitted in

#### Scenario: A send while the one-shot is in flight
- **WHEN** a send is initiated while another caller's send is in flight
- **THEN** it is accepted rather than refused, and the acknowledgement registry matches each acknowledgement to its own message
