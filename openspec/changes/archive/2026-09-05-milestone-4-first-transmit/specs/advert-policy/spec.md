## ADDED Requirements

> Reference: DESIGN.md §4.3 *Advert policy*, §12 milestone 4. The floor, the jitter, the
> inter-entity gap and the override are unchanged; these requirements only let a persistent
> identity drive them, and add the one-shot zero-hop advert the first-transmit exercise needs.

### Requirement: Adverts may be driven by a persistent entity identity
The system SHALL accept an entity whose keypair was loaded from storage as an advert source on
the same terms as an ephemeral stub, applying every existing interval, jitter, gap and override
rule unchanged, and SHALL distinguish the two in its output so an operator can tell which
identities outlive the process.

#### Scenario: A loaded identity adverts
- **WHEN** an entity loaded from a keyfile is added as an advert source
- **THEN** its adverts are signed with the stored key, scheduled under the same 24 hour floor, jitter and inter-entity gap as any other entity, and its startup listing marks it persistent rather than ephemeral

#### Scenario: Persistent and ephemeral entities in one run
- **WHEN** a run carries both a loaded identity and an ephemeral stub
- **THEN** both appear in the startup listing, each marked with whether its key survives the process, and the inter-entity advert gap applies between them

### Requirement: A single zero-hop advert can be requested explicitly
The system SHALL support emitting one zero-hop advert for a named entity on explicit request,
independent of that entity's schedule, and SHALL leave the entity's zero-hop interval disabled
by default afterwards. The request SHALL be submitted through the transmit scheduler as ordinary
class 3 traffic and SHALL be charged against the airtime budget like any other advert.

#### Scenario: One-shot zero-hop advert
- **WHEN** a zero-hop advert is explicitly requested for an entity
- **THEN** exactly one zero-hop advert is submitted at priority class 3, charged against the budget, and no recurring zero-hop schedule is created

#### Scenario: The request does not change the flood schedule
- **WHEN** a one-shot zero-hop advert is emitted
- **THEN** the entity's next flood advert time is unchanged, and the inter-entity flood gap is unaffected
