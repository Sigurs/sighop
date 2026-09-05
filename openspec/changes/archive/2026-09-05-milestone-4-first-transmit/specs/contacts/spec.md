## ADDED Requirements

> Reference: DESIGN.md §3 (node-hash collisions), §5 (advert verification), §6 (the `contact`
> table this in-memory store anticipates). Verification itself belongs to `mesh-crypto`; this
> capability decides what a verified advert becomes.

### Requirement: Only verified adverts become contacts
The system SHALL create or update a contact from an advert only after its Ed25519 signature has
verified, and SHALL discard an unsigned or badly-signed advert without recording any of its
content, including the name.

#### Scenario: Verified advert
- **WHEN** an advert whose signature verifies is received
- **THEN** a contact is created or updated with its public key, node hash, name, appdata flags and heard timestamps

#### Scenario: Advert with a bad signature
- **WHEN** an advert whose signature does not verify is received
- **THEN** no contact is created or updated, and the discard is reported

### Requirement: Contacts are keyed by public key
The system SHALL key contacts by their full 32-byte public key, and SHALL treat two adverts
carrying the same public key as the same contact regardless of the path they arrived by.

#### Scenario: Same peer heard twice by different paths
- **WHEN** two verified adverts carrying one public key arrive with different paths
- **THEN** one contact exists, its last-heard timestamp advances, and its first-heard timestamp is unchanged

#### Scenario: Name changes between adverts
- **WHEN** a verified advert carries a name different from the one recorded for that public key
- **THEN** the contact's name is updated and the change is reported, because the name is advert content and only the key is the identity

### Requirement: Node-hash lookup returns every candidate
The system SHALL index contacts by node hash and SHALL return the complete set of contacts
sharing a node hash, never a single contact. No consumer may treat a node hash as identifying a
peer.

#### Scenario: Two contacts share a node hash
- **WHEN** two contacts whose public keys share their first byte are held and a lookup is made by that node hash
- **THEN** both contacts are returned

#### Scenario: Node hash matches nothing
- **WHEN** a lookup is made for a node hash held by no contact
- **THEN** an empty set is returned rather than an error

### Requirement: A contact may be added from a public key alone
The system SHALL allow a contact to be added from a hex public key supplied by the operator,
for the case where the peer's advert has not been heard, and SHALL mark such a contact as
having no verified advert behind it.

#### Scenario: Manual addition
- **WHEN** a 32-byte hex public key is supplied
- **THEN** a contact is created with that key and its derived node hash, carrying no name and marked as unverified-by-advert

#### Scenario: Manual addition of a key already known
- **WHEN** a hex public key matching an existing contact is supplied
- **THEN** the existing contact is returned unchanged, keeping the name and flags its verified advert supplied

### Requirement: Contacts are selectable by name or key prefix
The system SHALL resolve a peer reference to a contact by exact name or by a hex public-key
prefix, and SHALL fail with an error listing the candidates when a reference matches more than
one contact.

#### Scenario: Unambiguous reference
- **WHEN** a peer reference matches exactly one contact by name or key prefix
- **THEN** that contact is selected

#### Scenario: Ambiguous reference
- **WHEN** a peer reference matches more than one contact
- **THEN** selection fails with an error naming every match, and no message is composed

### Requirement: The contact store is in-memory for this milestone
The system SHALL hold contacts in memory only, and SHALL state in its startup output that
contacts do not survive the process.

#### Scenario: Restart
- **WHEN** the runtime is restarted
- **THEN** the contact store is empty and the startup output says so
