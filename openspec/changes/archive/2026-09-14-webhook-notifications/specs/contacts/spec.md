## MODIFIED Requirements

### Requirement: Advert observations are reported to a listener
The system SHALL report every verified-advert observation to each of zero or more listeners, stating
whether the contact was created, whether anything worth persisting changed, and — when an advert
renamed a known key — the previous and current names, together with the reception that produced the
observation, including its hop count, its signal quality and its packet identity. Every listener
SHALL be invoked after the store has been updated and before the next observation is processed, so
that a first sighting is an ordered fact rather than a race between observers, and every listener
SHALL receive the same observation.

#### Scenario: A contact is created
- **WHEN** a verified advert creates a contact
- **THEN** each listener is invoked with that contact, marked as created, and with the reception's hop count and signal quality

#### Scenario: A contact is updated
- **WHEN** a verified advert updates a contact that already existed
- **THEN** each listener is invoked with the contact, not marked as created

#### Scenario: Ordering against the store
- **WHEN** a listener is invoked
- **THEN** the store already reflects the observation, so a lookup of that contact from within the listener finds it

#### Scenario: Several listeners
- **WHEN** two listeners are configured and a verified advert creates a contact
- **THEN** both are invoked with that observation marked as created, each exactly once

#### Scenario: No listener configured
- **WHEN** no listener is configured
- **THEN** observation behaviour, persistence and reporting are exactly as they were

### Requirement: A listener failure never costs a contact
The system SHALL treat each listener as untrusted with respect to the store and to every other
listener: a listener that raises SHALL have its failure reported and SHALL NOT prevent the contact
from being recorded, persisted, or reported, SHALL NOT prevent any other listener from receiving the
same observation, and SHALL NOT prevent later observations from being processed.

#### Scenario: The listener raises
- **WHEN** a listener raises on an observation
- **THEN** the contact is still stored and offered for persistence, the failure is reported, and the next advert is processed normally

#### Scenario: One of several listeners raises
- **WHEN** the first of two listeners raises on an observation
- **THEN** the second still receives that observation

#### Scenario: A slow listener
- **WHEN** a listener returns without completing work of its own
- **THEN** the store's own path is unaffected, because the listener is not the place where a subscriber's work is done
