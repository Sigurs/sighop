# Spec Delta

## ADDED Requirements

### Requirement: A webhook can be renamed without changing where it delivers
The system SHALL provide an action that changes a stored webhook's name and nothing else. The
webhook's target URL, format, triggers, hop limit, enabled state and recorded delivery outcomes
SHALL be unchanged by a rename, and a running process SHALL use the new name without a restart, on
the same terms as any other webhook configuration change.

The system SHALL refuse a rename whose new name is empty or only whitespace, and SHALL refuse a
rename to a name another webhook already holds, because webhook names are unique and webhooks are
addressed by name on the command line. A rename to the name the webhook already holds SHALL be
accepted and change nothing.

A rename SHALL NOT render a stored URL in full anywhere, including in a form re-shown after a
refused rename.

#### Scenario: Renaming a webhook
- **WHEN** a webhook is renamed
- **THEN** it is listed under its new name with its format, triggers, hop limit, enabled state and recorded delivery outcomes unchanged

#### Scenario: The target is untouched
- **WHEN** a webhook is renamed and an event it subscribes to then fires
- **THEN** it is delivered to the same target as before

#### Scenario: Renaming to a name already in use
- **WHEN** a rename is asked for with a name another webhook already holds
- **THEN** the rename is refused for the same reason adding a duplicate name gives, and nothing is changed

#### Scenario: Renaming to an empty name
- **WHEN** a rename is asked for with an empty or whitespace-only name
- **THEN** the rename is refused and nothing is changed

#### Scenario: A refused rename discloses nothing
- **WHEN** a rename is refused
- **THEN** the stored URL appears as scheme and host at most, and no URL path or query is present in what is shown
