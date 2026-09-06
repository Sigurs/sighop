## ADDED Requirements

> Reference: milestone 7 adds a component that must act on the *first* sighting of a peer. The
> store already knows whether an advert created a contact; it just never told anyone. Nothing about
> keying, candidate sets, verification or durability changes.

### Requirement: Advert observations are reported to a listener
The system SHALL report every verified-advert observation to an optional listener, stating whether
the contact was created, whether anything worth persisting changed, and — when an advert renamed a
known key — the previous and current names, together with the reception that produced the
observation, including its hop count, its signal quality and its packet identity. The listener
SHALL be invoked after the store has been updated and before the next observation is processed, so
that a first sighting is an ordered fact rather than a race between observers.

#### Scenario: A contact is created
- **WHEN** a verified advert creates a contact
- **THEN** the listener is invoked with that contact, marked as created, and with the reception's hop count and signal quality

#### Scenario: A contact is updated
- **WHEN** a verified advert updates a contact that already existed
- **THEN** the listener is invoked with the contact, not marked as created

#### Scenario: Ordering against the store
- **WHEN** the listener is invoked
- **THEN** the store already reflects the observation, so a lookup of that contact from within the listener finds it

#### Scenario: No listener configured
- **WHEN** no listener is configured
- **THEN** observation behaviour, persistence and reporting are exactly as they were

### Requirement: A listener failure never costs a contact
The system SHALL treat a listener as untrusted with respect to the store: a listener that raises
SHALL have its failure reported and SHALL NOT prevent the contact from being recorded, persisted,
or reported, and SHALL NOT prevent later observations from being processed.

#### Scenario: The listener raises
- **WHEN** a listener raises on an observation
- **THEN** the contact is still stored and offered for persistence, the failure is reported, and the next advert is processed normally

#### Scenario: A slow listener
- **WHEN** a listener returns without completing work of its own
- **THEN** the store's own path is unaffected, because the listener is not the place where a subscriber's work is done
