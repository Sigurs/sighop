# entity-store Specification

## Purpose
How local entity identities are held in the database with their private keys encrypted at rest,
so that a database dump is not sufficient to impersonate a room server, and how identities move
between a keyfile and that store.

## Requirements

### Requirement: A private key is never stored in the clear
The system SHALL encrypt an entity's private key before it reaches the database, using a secret
supplied by the environment and held nowhere in the database, and SHALL store no representation
from which the private key can be recovered without that secret. A stored key SHALL carry an
authentication tag, so tampering with the stored value is detected rather than yielding a
different key.

#### Scenario: Storing an identity
- **WHEN** an entity identity is written to the database
- **THEN** the stored key material is ciphertext, the plaintext private key appears in no column, and the row alone is insufficient to reconstruct the identity

#### Scenario: Stored key material is altered
- **WHEN** stored key material is modified and then loaded
- **THEN** loading fails with an authentication error naming the entity, and no key is produced

#### Scenario: Loaded identity matches the stored public key
- **WHEN** an entity is loaded and its private key decrypted
- **THEN** the public key derived from that private key is compared against the public key stored beside it, and a mismatch is an error naming the entity rather than a silently preferred value

### Requirement: The encryption secret is required, validated, and never derived from a passphrase
The system SHALL require the encryption secret to be present and of the exact expected length
whenever entity identities are read from or written to the database, SHALL fail at startup with a
message naming the missing or malformed variable, and SHALL NOT derive the secret from a
passphrase or generate one on the fly. The system SHALL provide a way to generate a
correctly-formed secret.

#### Scenario: Secret missing
- **WHEN** the runtime starts with a database configured, entities persisted, and no encryption secret in the environment
- **THEN** startup fails naming the environment variable, and no entity is loaded

#### Scenario: Secret malformed
- **WHEN** the supplied secret is not of the expected length or encoding
- **THEN** startup fails saying which it is, and does not pad, truncate or hash the value into shape

#### Scenario: Secret does not open the stored keys
- **WHEN** a secret of the correct form does not decrypt an existing entity
- **THEN** the failure is reported as a wrong-secret error naming the entity, distinct from a corrupt-data error

#### Scenario: Generating a secret
- **WHEN** the operator asks for a new encryption secret
- **THEN** a correctly-formed secret is printed once, together with the statement that losing it makes every stored identity unrecoverable

### Requirement: Persisted entities carry their identity and advert configuration
The system SHALL store, for each local entity, a stable identifier, the entity type, the name,
the public key, the node hash, the encrypted private key, its advert configuration, and whether it
is enabled; and SHALL restore all of it on the next run so that a public key published to another
node stays valid and adverts resume on their configured schedule.

#### Scenario: Restart with persisted entities
- **WHEN** the runtime restarts against a database holding entities
- **THEN** each enabled entity is loaded with the same public key and node hash it had before, its advert configuration is restored, and the loaded entities are reported at startup

#### Scenario: Disabled entity
- **WHEN** a persisted entity is marked disabled
- **THEN** it is not loaded as an originating identity, it adverts nothing, and its presence is still reported

### Requirement: Node hashes do not collide across persisted and loaded entities
The system SHALL apply the node-hash collision rule across every local entity in one run,
whichever source it came from, SHALL refuse to start when two of them share a node hash, naming
both, and SHALL reject a newly generated keypair that collides with any of them.

#### Scenario: A new entity collides with a persisted one
- **WHEN** an entity is created while a persisted entity with the same node hash exists
- **THEN** the keypair is discarded and generation retries until a non-colliding node hash is found

#### Scenario: Two persisted entities collide
- **WHEN** the database holds two enabled entities whose public keys share their first byte
- **THEN** startup fails naming both entities and their shared node hash

### Requirement: Identities can be imported and exported deliberately
The system SHALL provide an explicit action to import an identity into the store — from a
keyfile, or from a private key the operator supplies directly — and an explicit action to export
a stored identity back to a keyfile, so an operator can bring an identity in and back it up on
purpose. Importing a supplied private key SHALL write no keyfile, so an operator moving an
identity into the store is not required to leave unencrypted key material on disk. Export SHALL
be a distinct command from inspection, SHALL state that the written file contains private key
material, and SHALL write it with owner-only permissions.

#### Scenario: Importing a keyfile
- **WHEN** an identity keyfile is imported
- **THEN** an entity row is created with the private key encrypted, the same public key and node hash, and the operation reports the public key it stored

#### Scenario: Importing a supplied private key
- **WHEN** a 64-byte private key is supplied to the import action directly
- **THEN** an entity row is created with that private key encrypted, the operation reports the public key it derived, and no keyfile is written at any point

#### Scenario: Importing an identity already stored
- **WHEN** a keyfile whose public key already exists in the store is imported
- **THEN** the import fails naming the existing entity, and the stored row is unchanged

#### Scenario: A supplied private key already stored
- **WHEN** a private key whose public key already exists in the store is supplied to the import action
- **THEN** the import fails naming the existing entity, and the stored row is unchanged

#### Scenario: Exporting an identity
- **WHEN** a stored identity is exported to a path
- **THEN** a keyfile readable by the owner only is written, the output states that it holds private key material, and the stored row is unchanged

#### Scenario: Export targets an existing file
- **WHEN** export is asked to write to a path that already exists
- **THEN** it fails naming the path, and the file's contents are unchanged

### Requirement: Inspection never discloses key material
The system SHALL provide listing and inspection of stored entities — name, type, public key, node
hash, advert configuration, enabled state — that discloses neither the private key nor its
ciphertext, and SHALL keep key material out of log events, error messages and status output.

#### Scenario: Listing stored entities
- **WHEN** stored entities are listed
- **THEN** each is shown with its name, type, public key and node hash, and neither the private key nor the stored ciphertext appears

#### Scenario: An entity operation fails
- **WHEN** an entity operation fails and is logged
- **THEN** the event names the entity and the failure, and contains no key material and no encryption secret

### Requirement: An entity can be created as a room server
The system SHALL allow a stored entity to be created as a room server, recording it with the
room-server entity type and with the room-server node type in its advert configuration, so that its
adverts identify it to the mesh as a room server without any further configuration. The choice
SHALL be explicit at creation; an entity SHALL NOT become a room server as a side effect of having
a room bound to it.

#### Scenario: Creating a room server identity
- **WHEN** an identity is created as a room server
- **THEN** it is stored with the room-server entity type, its advert configuration carries the room-server node type, and inspecting it reports both

#### Scenario: Creating an ordinary identity
- **WHEN** an identity is created without a type given
- **THEN** it is stored exactly as before this capability changed, as an ordinary chat identity

#### Scenario: An imported keyfile declaring a node type
- **WHEN** a keyfile declaring the room-server node type is imported
- **THEN** the stored entity carries that node type and the room-server entity type, rather than being coerced to the default

### Requirement: A room server identity's private key is protected exactly as any other
The system SHALL seal a room server identity's private key under the same environment secret, with
the same refusal to start on a missing or malformed secret, and SHALL apply the same node-hash
collision rule across room server and ordinary entities, so that being a room server changes
nothing about how the identity is stored or checked.

#### Scenario: A room server seed at rest
- **WHEN** a room server identity is stored
- **THEN** its private key is sealed exactly as any other entity's, and a database dump yields no usable key

#### Scenario: A room server colliding with an ordinary entity
- **WHEN** a room server identity and another entity share a node hash
- **THEN** startup fails naming both and the shared hash, exactly as for two ordinary entities

### Requirement: An entity can be created as a bot
The system SHALL allow a stored entity to be created as a bot, recording it with the bot entity
type and with the ordinary chat node type in its advert configuration, because a bot presents
itself to the mesh as a chat node and its automation is sighop's business rather than the mesh's.
The choice SHALL be explicit at creation; an entity SHALL NOT become a bot as a side effect of
having a bot bound to it.

#### Scenario: Creating a bot identity
- **WHEN** an identity is created as a bot
- **THEN** it is stored with the bot entity type, its advert configuration carries the chat node type, and inspecting it reports both

#### Scenario: A bot identity on the wire
- **WHEN** a bot identity adverts
- **THEN** the advert declares the chat node type, indistinguishable from a companion's

#### Scenario: An entity that is already a room server
- **WHEN** a bot is bound to an entity stored as a room server
- **THEN** the binding is refused and says the entity already has a role

### Requirement: A bot identity's private key is protected exactly as any other
The system SHALL seal a bot identity's private key under the same environment secret, with the
same refusal to start on a missing or malformed secret, and SHALL apply the same node-hash
collision rule across bot, room server and ordinary entities, so that being a bot changes nothing
about how the identity is stored or checked.

#### Scenario: A bot seed at rest
- **WHEN** a bot identity is stored
- **THEN** its private key is sealed exactly as any other entity's, and a database dump alone does not disclose it

#### Scenario: A bot identity colliding with an existing node hash
- **WHEN** a bot identity is created whose node hash collides with an existing entity's
- **THEN** it is refused by the same rule that governs any other entity

### Requirement: Key material stored under the removed format is refused by name
The system SHALL refuse an entity row whose sealed key material was written under the removed
format that sealed a 32-byte seed, and SHALL say that is what happened rather than reporting the
row as corrupt or as failing to authenticate — the operator needs to know the row is intact and
the format is gone, because those call for different actions. The system SHALL NOT convert such a
row, and SHALL offer no command that does.

#### Scenario: A row sealed under the removed format
- **WHEN** an entity row whose sealed value holds a 32-byte seed is loaded
- **THEN** it is refused naming the entity and stating that the seed format is no longer supported, the row is left unchanged, and no identity is produced

#### Scenario: The refusal is distinguishable from corruption
- **WHEN** a row under the removed format and a row whose ciphertext has been altered are each loaded
- **THEN** the two produce different messages, and neither is reported as the other

#### Scenario: A run holding both kinds of row
- **WHEN** a run loads identities and one stored row is under the removed format
- **THEN** that row is refused by name and the run reports it, and the identities that do open are still loaded

#### Scenario: Newly stored key material
- **WHEN** an identity is stored after this change
- **THEN** the sealed value holds the 64-byte private key, and opening it needs the same secret as before

### Requirement: A stored identity can be removed deliberately
The system SHALL provide an explicit action to remove a stored identity, so that an identity
under the removed seed format can be replaced by the same identity supplied as a private key —
without which the stranded row blocks its own recovery, because the public key it holds is
already taken. Removal SHALL be irreversible and SHALL say so before it happens.

The system SHALL refuse to remove an identity that a room or a bot is bound to, naming what it
serves, because those rows are deleted with it and a room's history is not something an operator
can be assumed to have meant to discard. The system SHALL require confirmation that names the
identity, or an explicit flag accepting the loss where no terminal is available, and SHALL remove
nothing when confirmation is absent or does not match.

#### Scenario: Removing an identity
- **WHEN** a stored identity that nothing is bound to is removed with confirmation
- **THEN** the row is deleted, the removal is reported, and the identity no longer appears in the listing

#### Scenario: Removing an identity a room or bot is bound to
- **WHEN** removal is asked for an identity that a room or a bot is bound to
- **THEN** it fails naming what the identity serves, and the row is unchanged

#### Scenario: Removal without confirmation
- **WHEN** removal is asked for without confirmation and without the flag that accepts the loss
- **THEN** nothing is removed and the output states what the removal would cost

#### Scenario: Confirmation that does not match
- **WHEN** removal is confirmed with text that is not the identity's name
- **THEN** nothing is removed and the output says it was not confirmed

#### Scenario: Removing a row under the removed format
- **WHEN** removal is asked for an identity whose stored key material is a seed
- **THEN** it succeeds, and the output states that the row's key material could not be read anyway so nothing usable was lost

#### Scenario: Re-importing after removal
- **WHEN** an identity is removed and the same private key is then imported
- **THEN** the import succeeds and the stored identity has the public key and node hash it had before

### Requirement: The schema migration states what will stop opening and reads no key material
The system SHALL apply the schema change for this change without reading, decrypting or
rewriting any key material, so that it runs with no encryption secret available, and SHALL report
how many stored entity rows are under the removed format and will therefore stop opening. The
count SHALL be a count only: no name, no public key and no ciphertext.

#### Scenario: Migrating with rows under the removed format present
- **WHEN** the schema migration is applied against a database holding rows under the removed format
- **THEN** it succeeds, reports how many rows will stop opening, and alters no row's key material

#### Scenario: Migrating without the encryption secret
- **WHEN** the schema migration is applied with no encryption secret available
- **THEN** it succeeds, because it reads and rewrites no key material

#### Scenario: The report discloses nothing
- **WHEN** the migration reports affected rows
- **THEN** the output carries a count and no entity name, public key or stored ciphertext

### Requirement: An identity's name is validated wherever it is set
The system SHALL apply one validation to an identity's name at every point that sets it —
creating, importing and renaming — so that a name the store accepts is a name every surface can
use. The validation SHALL refuse a name that is empty or only whitespace, one longer than the
limit the store sets, one containing a control or unassigned character, and one containing the
separator that a channel post places between the sender's name and its text, because a receiver
splits a channel message at the first separator and an identity carrying one could never post.

The refusal SHALL name the reason, and SHALL be the same reason whichever surface asked. Nothing
SHALL be stored or changed when a name is refused. A name SHALL be stored with surrounding
whitespace stripped.

#### Scenario: Creating an identity with an empty name
- **WHEN** an identity is created with an empty or whitespace-only name
- **THEN** the creation is refused saying a name cannot be empty, and nothing is stored

#### Scenario: Creating an identity with a name that could never post
- **WHEN** an identity is created with a name containing the channel-post separator
- **THEN** the creation is refused saying that a receiver would read part of the name as the message, and nothing is stored

#### Scenario: Importing an identity under a refused name
- **WHEN** an identity is imported from a keyfile whose name the validation refuses
- **THEN** the import is refused for that reason and no row is written

#### Scenario: A control character in a name
- **WHEN** a name containing a control or unassigned character is submitted at create or at rename
- **THEN** it is refused naming the code point, and nothing is stored or changed

#### Scenario: The same reason from either surface
- **WHEN** the same refused name is submitted through the command line and through the interface
- **THEN** both give the same reason

#### Scenario: Surrounding whitespace
- **WHEN** an identity is created or renamed with a name carrying leading or trailing whitespace
- **THEN** the stored name has that whitespace stripped

### Requirement: A stored identity can be renamed
The system SHALL provide an action that changes a stored identity's name and nothing else. The
identity's public key, node hash, sealed key material, type, advert configuration, enabled state
and creation time SHALL be unchanged by a rename, so that a renamed identity is the same identity
to the mesh and to every row that refers to it.

The system SHALL refuse a rename whose new name is empty or only whitespace, applying the same
validation the create path applies to a name. The system SHALL refuse a rename to a name another
stored identity already holds, because identities are addressed by exact name on the command line
and an ambiguous name makes an identity unaddressable. A rename to the name the identity already
holds SHALL be accepted and change nothing.

Renaming SHALL be reversible by renaming back, and the system SHALL NOT require confirmation of a
rename in the way removal is confirmed.

#### Scenario: Renaming an identity
- **WHEN** a stored identity is renamed to a name no other identity holds
- **THEN** the identity is listed under its new name, and its public key, node hash, type and stored key material are unchanged

#### Scenario: Renaming to a name already in use
- **WHEN** a rename is asked for with a name another stored identity already holds
- **THEN** the rename is refused naming the identity that holds it, and nothing is changed

#### Scenario: Renaming to an empty name
- **WHEN** a rename is asked for with an empty or whitespace-only name
- **THEN** the rename is refused for the same reason the create path gives, and nothing is changed

#### Scenario: Renaming to the same name
- **WHEN** a rename is asked for with the name the identity already holds
- **THEN** it is accepted and the identity is unchanged

#### Scenario: A rename reads no key material
- **WHEN** an identity is renamed while the sealing secret is not configured
- **THEN** the rename succeeds, because renaming neither opens nor rewrites the sealed key material

#### Scenario: What a rename does not change
- **WHEN** a room or a bot is bound to an identity that is then renamed
- **THEN** the binding is unchanged and the room or bot is still bound to the same identity

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
