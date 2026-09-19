# Spec Delta

## ADDED Requirements

### Requirement: The feed summarises node discovery frames
The system SHALL show, in the live feed's detail column, a one-line summary of each node discovery
frame received live: for a request, the tag and the node types the filter selects; for a
response, the tag, the responder node type, the SNR it reports and a claimed key prefix. The
claimed key SHALL be labelled as unauthenticated, per the requirement that unverified content is
never presented as verified. A discovery frame replayed from recorded history SHALL still show
its discovery outcome. The summary is live-only, because it is not persisted.

#### Scenario: A discovery response arrives while the feed is open
- **WHEN** a discovery response is received with a browser connected
- **THEN** its feed row shows the outcome `discover_response` and a detail summary carrying the tag, the node type, the reported SNR and a key prefix labelled as unauthenticated

#### Scenario: A discovery request and its responses
- **WHEN** a discovery request and a response to it are received in turn
- **THEN** both rows show the same tag, so the pairing can be read from the feed

#### Scenario: A discovery frame in recorded history
- **WHEN** the feed paints recorded history containing a discovery response
- **THEN** the row shows the outcome `discover_response`, not `uninterpreted_payload`
