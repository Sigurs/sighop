# Spec Delta

## MODIFIED Requirements

### Requirement: The station's radio, schema and gate controls are on one system page
The system SHALL present, on a single system page, the radio parameters in force as the board's
readback reports them, the schema revision agreement, the capabilities deliberately left to the
terminal (applying migrations, managing accounts, generating the sealing secret), a link to the
confirmation view for enabling transmission and to the confirmation view for raising the airtime
ceiling, and the repeater collection settings. The board's readback SHALL appear on that page once.
No other page SHALL repeat the readback table or the schema revision table.

#### Scenario: Opening the system page
- **WHEN** the system page is opened
- **THEN** it shows the board's readback, the applied and expected schema revisions and whether they agree, the terminal-only capabilities with their commands, links to the enable-transmission and raise-ceiling confirmations, and the repeater collection settings

#### Scenario: Reaching the gate controls
- **WHEN** an operator follows the enable-transmission or raise-ceiling link on the system page
- **THEN** the confirmation view for that action is shown, and nothing is enabled or raised by following the link

#### Scenario: A replay has no readback
- **WHEN** the system page is opened on a run that took no startup probe
- **THEN** it states that there is no readback because there was no board to ask, and still shows the schema revision and the gate links

## ADDED Requirements

### Requirement: Repeater collection is configured on the system page
The system SHALL offer, on the system page, a form for the repeater collection settings: enabled,
the login identity (chosen from the stored identities that are not serving a room), the interval
in minutes, the recency window in days, and the retention window in days. The form SHALL state
that collection logs in with a blank guest password, that only repeaters selected on the contacts
page are polled, and how many repeaters are selected and how many of those are within the recency
window now. The interval SHALL be accepted from 5 to 1440 minutes, the recency window from 1 to 365
days, and the retention window from 1 to 365 days. A value outside its range or not a whole number
SHALL be refused with the reason and nothing stored. A saved change SHALL apply to the running
process without a restart. Collection SHALL NOT be enabled while transmission is disabled without
the form stating that no poll will be sent until transmission is enabled.

#### Scenario: Saving valid settings
- **WHEN** an operator enables collection with an identity, a 30-minute interval, a 2-day recency window and a 14-day retention window
- **THEN** the settings are stored, the next cycle uses them without a restart, and the page shows them

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
