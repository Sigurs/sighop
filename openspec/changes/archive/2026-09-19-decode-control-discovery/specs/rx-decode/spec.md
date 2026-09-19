# Spec Delta

## ADDED Requirements

### Requirement: Node discovery frames are reported as discovery, with the responder unauthenticated
The system SHALL report a reception whose payload parses as a node discovery request or response
with its own outcome, `discover_request` or `discover_response`, and not as an uninterpreted
payload or as an encrypted payload with no key held. The *Packet RX* wide event for such a frame
SHALL carry the discovery tag. A request's event SHALL also carry the node-type filter and the
prefix-only flag. A response's event SHALL also carry the responder node type, the SNR the
responder reports for the request, and a prefix of the claimed public key.

The claimed public key in a discovery response SHALL be treated as unauthenticated. Decoding it
SHALL NOT create or update a contact, a learned path, or any other record keyed by identity.

#### Scenario: A discovery request is received
- **WHEN** a DIRECT, zero-hop CONTROL frame carrying a discovery request is decoded
- **THEN** the record's outcome is `discover_request`, and the wide event carries the tag and the filter with no decrypt outcome

#### Scenario: A discovery response is received
- **WHEN** a CONTROL frame carrying a discovery response is decoded
- **THEN** the record's outcome is `discover_response`, and the wide event carries the tag, the node type, the reported SNR and the claimed key prefix

#### Scenario: A response claims the key of a known contact
- **WHEN** a discovery response's claimed key equals the public key of a verified contact
- **THEN** no contact, path or signal record is created or changed by that reception

#### Scenario: A malformed discovery payload
- **WHEN** a CONTROL frame's payload has a discovery subtype but an invalid length
- **THEN** the record carries the decoded packet together with the payload failure, per the existing payload-failure outcome
