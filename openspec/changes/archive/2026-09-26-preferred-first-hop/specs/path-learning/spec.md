## MODIFIED Requirements

### Requirement: The most recently confirmed path wins
The system SHALL resolve a lookup for a destination to its most recently confirmed path, and
SHALL retain the SNR and hop count of each candidate without applying any further scoring. The
single exception is route selection for sending while a preferred first hop is set, which the
`route-preference` capability defines; learning, storage and the recorded candidates are unaffected
by it.

#### Scenario: Two routes observed to one peer
- **WHEN** two different paths have been learned for the same sender
- **THEN** a lookup returns the one most recently confirmed, and both remain recorded

#### Scenario: Lookup for an unknown destination
- **WHEN** a lookup is made for a destination with no learned path
- **THEN** the store reports no path rather than returning an empty path, so a caller cannot mistake "unknown" for "zero hops"

#### Scenario: Preferred first hop set
- **WHEN** a preferred first hop is set and a route is chosen for sending
- **THEN** the choice follows the `route-preference` rules, and the candidates recorded for the destination are unchanged
