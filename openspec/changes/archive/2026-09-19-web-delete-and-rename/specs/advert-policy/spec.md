# Spec Delta

## ADDED Requirements

### Requirement: Renaming a loaded identity changes the name its adverts carry without resetting its schedule
The system SHALL apply a rename of a stored identity to the running process that loaded it,
without a restart, so that every advert built after the rename carries the new name and no advert
carries a name the store no longer holds.

A rename SHALL NOT reset, advance or re-jitter that identity's flood schedule: its next scheduled
flood, the adverts it has already sent in this run, any active override and the inter-entity gap
SHALL all be exactly what they were before the rename. A rename SHALL NOT itself transmit
anything.

A rename SHALL NOT change the identity's public key or node hash, so it SHALL NOT be refused for a
node-hash collision and SHALL NOT create one. The system SHALL refuse a rename that would give two
identities loaded by the same run the same name, because a run addresses its loaded identities by
name.

A stored identity that this run has not loaded SHALL be renamable in the store, and the rename
SHALL have no effect on this run.

#### Scenario: The next advert carries the new name
- **WHEN** a loaded identity is renamed and an advert for it is then built
- **THEN** that advert carries the new name

#### Scenario: The schedule survives the rename
- **WHEN** a loaded identity with a scheduled flood is renamed
- **THEN** its next scheduled flood, its advert count for this run and any active override are unchanged

#### Scenario: A rename transmits nothing
- **WHEN** a loaded identity is renamed and no advert is requested
- **THEN** nothing is submitted to the radio and no airtime is charged

#### Scenario: A rename colliding with another loaded identity
- **WHEN** a rename would give a second identity loaded by this run the same name
- **THEN** the rename is refused and neither identity is changed

#### Scenario: Renaming an identity this run has not loaded
- **WHEN** a stored identity that this run did not load is renamed
- **THEN** the store holds the new name and this run's loaded identities and schedules are unchanged

#### Scenario: A requested advert after a rename
- **WHEN** an advert is requested for a loaded identity immediately after it is renamed
- **THEN** it is decided by the rules that already govern a requested advert, and the advert it submits carries the new name
