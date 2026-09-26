## ADDED Requirements

### Requirement: The preferred first hop is configured on the system page
The system SHALL offer, on the system page, a form to set or clear the preferred first hop,
choosing from the known contacts whose advertised node type is repeater. The form SHALL state that
the setting applies to every DIRECT send from every identity, and SHALL show, for the repeater in
force, its name, abbreviated public key, and whether a zero-hop route to it has been learned and
when it was last confirmed. When no zero-hop route to the chosen repeater is known, the page SHALL
warn that the node may not reach it directly. A saved change SHALL apply to the running process
without a restart. A submitted key that is not a known repeater contact SHALL be refused with the
reason and nothing stored.

#### Scenario: Choosing a repeater
- **WHEN** an operator chooses a known repeater and saves
- **THEN** it is stored, the next DIRECT send goes through it without a restart, and the page shows it as in force

#### Scenario: Clearing the setting
- **WHEN** an operator chooses none and saves
- **THEN** the setting is cleared and routes are chosen as learned again

#### Scenario: Repeater not heard directly
- **WHEN** the repeater in force has no learned zero-hop route
- **THEN** the page warns that the node may not reach it directly

#### Scenario: Unknown key submitted
- **WHEN** a form is submitted naming a public key that is not a known repeater contact
- **THEN** it is refused with the reason and the stored setting is unchanged

#### Scenario: Preferred repeater's contact later forgotten
- **WHEN** the preferred repeater is no longer among the known contacts
- **THEN** the setting stays in force by public key and the page shows it by abbreviated key, with the warning
