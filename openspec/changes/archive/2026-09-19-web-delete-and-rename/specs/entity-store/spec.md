# Spec Delta

## ADDED Requirements

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
