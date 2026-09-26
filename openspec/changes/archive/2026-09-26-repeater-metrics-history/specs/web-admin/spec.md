# Spec Delta

## MODIFIED Requirements

### Requirement: Repeater collection is configured on the system page
The system SHALL offer, on the system page, a form for the repeater collection settings: enabled,
the login identity (chosen from the stored identities that are not serving a room), the interval
in minutes, the recency window in days, and the retention window. The form SHALL state
that collection logs in with a blank guest password, that only repeaters selected on the contacts
page are polled, and how many repeaters are selected and how many of those are within the recency
window now. The interval SHALL be accepted from 5 to 1440 minutes, the recency window from 1 to 365
days, and the retention window from 1 to 365 days or keep forever, keep forever being offered as the
retention window's maximum and stating that nothing collected is then ever deleted. A value outside
its range or not a whole number SHALL be refused with the reason and nothing stored; a blank
retention field SHALL NOT be read as keep forever. A saved change SHALL apply to the running
process without a restart. Collection SHALL NOT be enabled while transmission is disabled without
the form stating that no poll will be sent until transmission is enabled.

#### Scenario: Saving valid settings
- **WHEN** an operator enables collection with an identity, a 30-minute interval, a 2-day recency window and a 14-day retention window
- **THEN** the settings are stored, the next cycle uses them without a restart, and the page shows them

#### Scenario: Choosing keep forever
- **WHEN** an operator chooses keep forever for the retention window and saves
- **THEN** the setting is stored, the page shows the retention window as kept forever, and no pruning deletes anything from then on

#### Scenario: A blank retention window
- **WHEN** an operator clears the retention days and does not choose keep forever
- **THEN** the form is refused with the reason and the stored settings are unchanged

#### Scenario: An out-of-range interval
- **WHEN** an operator submits an interval of 2 minutes
- **THEN** the form is refused with the allowed range and the stored settings are unchanged

#### Scenario: Junk in a numeric field
- **WHEN** an operator submits a recency window of "abc"
- **THEN** the form is refused with the reason and the stored settings are unchanged, rather than the field being treated as unset

#### Scenario: Enabled with transmission off
- **WHEN** collection is enabled while transmission is disabled
- **THEN** the system page states that no poll will be sent until transmission is enabled

#### Scenario: Last cycle shown
- **WHEN** the system page is opened after a cycle has run
- **THEN** it shows when the last cycle started, how many repeaters it polled, and how many of those succeeded
