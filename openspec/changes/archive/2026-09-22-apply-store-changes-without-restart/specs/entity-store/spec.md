# Spec Delta

## ADDED Requirements

### Requirement: A running process uses changed stored identities without a restart

The system SHALL hold its local identities in memory, loaded at startup, SHALL apply a change to
the stored identities made through the web interface of the same run immediately, and SHALL apply a
change made by another process within 60 seconds. The changes covered are an identity added,
imported, enabled, disabled or removed, and a change to a stored identity's advert configuration.

An identity that becomes loadable — newly stored and enabled, or an existing one enabled — SHALL be
adopted into the run and SHALL from that moment originate adverts, match inbound packets addressed
to its node hash, be readable as a path body source, post to channels and be composable from in
chat, on exactly the terms an identity loaded at startup is. An identity that stops being loadable
— disabled or removed — SHALL be withdrawn from all of the same, and SHALL originate no further
advert.

A re-read that adopts a different set of identities SHALL be reported, naming the identities
adopted and withdrawn; a re-read that changes nothing SHALL be silent. When the stored identities
cannot be read, the system SHALL keep the set it has loaded and report the failure, because memory
is the authority while durable storage is degraded.

Adoption SHALL NOT read key material into any output, and a withdrawal SHALL NOT be reported in
terms that disclose the private key of the identity withdrawn.

#### Scenario: An identity created from the command line

- **WHEN** an identity is stored and enabled with the command line while a run is active
- **THEN** within 60 seconds that run holds it, adverts for it, and reports that the identity was adopted

#### Scenario: An identity created through this run's own interface

- **WHEN** an identity is created through the web interface of the running process
- **THEN** that run holds it without waiting for the periodic re-read and without a restart

#### Scenario: An identity disabled

- **WHEN** a loaded identity is disabled
- **THEN** the run withdraws it, no further advert is originated for it, and no packet addressed to its node hash is treated as addressed to this station

#### Scenario: An identity removed

- **WHEN** a loaded identity is removed from the store
- **THEN** the run withdraws it on the same terms as a disabled one, and the withdrawal is reported

#### Scenario: A refresh that finds no change

- **WHEN** the stored identities are re-read and match the set in force
- **THEN** nothing is reported, because the station's state did not change

#### Scenario: The database is unreachable at refresh

- **WHEN** a re-read of the stored identities fails because the database is degraded
- **THEN** every identity already loaded keeps advertising and keeps matching inbound packets, and the failure is reported

#### Scenario: An identity stored but not enabled

- **WHEN** an identity is stored with its enabled state off
- **THEN** it is not adopted, it adverts nothing, and its presence is still reported

### Requirement: A live adoption that would collide on node hash is refused, not fatal

The system SHALL apply the node-hash collision rule to an identity offered for adoption mid-run,
and SHALL refuse that adoption naming both the identity offered and the loaded identity it collides
with. A refused adoption SHALL leave every loaded identity exactly as it was and SHALL NOT end the
run, because a run that is on the air must not be stopped by a write another process made to the
store.

The system SHALL keep refusing that identity on each subsequent re-read while the collision stands,
and SHALL NOT report the same refusal repeatedly as though it were new.

#### Scenario: A stored identity colliding with a loaded keyfile

- **WHEN** another process stores an enabled identity whose node hash matches one this run loaded from a keyfile
- **THEN** the adoption is refused naming both, the run continues with the identities it had, and nothing is transmitted differently

#### Scenario: The refusal is not repeated on every refresh

- **WHEN** a colliding identity is still stored at the next periodic re-read
- **THEN** it is still not adopted and the refusal is not reported again

#### Scenario: A collision resolved

- **WHEN** the identity a refused adoption collided with is withdrawn, and the refused identity is still stored and enabled
- **THEN** it is adopted at the next re-read and its adoption is reported
