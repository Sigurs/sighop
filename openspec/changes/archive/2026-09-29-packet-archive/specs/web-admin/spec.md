# Spec Delta

## ADDED Requirements

### Requirement: The packet archive is shown, configured and exported on the system page
The system SHALL show, on the system page, the packet archive's state: how many records it
holds, the times of the oldest and newest, how many monthly partitions exist, how many records
this run has written and discarded, and the retention setting. The page SHALL offer a retention
form whose value is either keep forever or an age bound of 30 to 3650 whole days. Keep forever
SHALL be the default, and the form SHALL say that nothing archived is then ever deleted. The form
SHALL say that removal is by whole month, so records may be kept up to a month past the bound.
A value outside the range or not a whole number SHALL be refused with the reason and nothing
stored. A blank field SHALL NOT be read as keep forever. A saved change SHALL apply to the running
process without a restart.

The page SHALL also offer an export of a time range. The export SHALL be downloaded as a
capture-format JSONL file and SHALL be available only to a signed-in operator. A range whose end
is not after its start SHALL be refused with the reason.

#### Scenario: Viewing archive state
- **WHEN** a signed-in operator opens the system page
- **THEN** it shows the archive's record count, oldest and newest record times, partition count, written and discarded counts, and retention setting

#### Scenario: Setting an age bound
- **WHEN** an operator sets retention to 365 days and saves
- **THEN** the setting is stored, the page shows it, and the next retention pass uses it without a restart

#### Scenario: Choosing keep forever
- **WHEN** an operator chooses keep forever and saves
- **THEN** the setting is stored, the page shows the archive as kept forever, and no retention pass removes anything from then on

#### Scenario: An out-of-range bound
- **WHEN** an operator submits a retention bound of 7 days
- **THEN** the form is refused with the allowed range and the stored setting is unchanged

#### Scenario: A blank bound
- **WHEN** an operator clears the days field and does not choose keep forever
- **THEN** the form is refused with the reason and the stored setting is unchanged

#### Scenario: Exporting a range
- **WHEN** a signed-in operator exports a one-day range
- **THEN** a JSONL capture file for that range is downloaded, starting with a header that says it came from the archive

#### Scenario: Export without signing in
- **WHEN** a request for an export arrives without a signed-in session
- **THEN** it is refused and no archive data is returned

#### Scenario: An inverted range
- **WHEN** an operator requests an export whose end is before its start
- **THEN** the request is refused with the reason
