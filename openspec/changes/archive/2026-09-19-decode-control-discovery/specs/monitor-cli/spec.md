# Spec Delta

## ADDED Requirements

### Requirement: Node discovery frames are rendered with their fields
The system SHALL render the detail line of a node discovery frame with what the frame carries,
not as an uninterpreted payload. A request's line SHALL show the tag, the node types the filter
selects, whether a key prefix was asked for, and `since` when present. A response's line SHALL
show the tag, the responder node type, the SNR it reports for the request, and the claimed
public key. The line SHALL state that the key is unauthenticated and SHALL NOT use the
verification mark reserved for verified identities.

#### Scenario: A discovery request is rendered
- **WHEN** a discovery request with filter `0x06` and tag `9a7d3916` is decoded
- **THEN** its detail line reads as a discovery request, names CHAT and REPEATER as the filter, and shows the tag

#### Scenario: A discovery response is rendered
- **WHEN** a discovery response from a repeater is decoded
- **THEN** its detail line shows the tag, REPEATER, the reported SNR in dB and the claimed key, marked unauthenticated and without the verified mark
