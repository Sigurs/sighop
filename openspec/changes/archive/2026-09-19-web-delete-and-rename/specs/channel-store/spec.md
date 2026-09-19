# Spec Delta

## ADDED Requirements

### Requirement: A channel can be renamed without changing what it opens
The system SHALL provide an action that changes a stored channel's name and nothing else. The
channel's kind, hashtag, stored or derived key, channel hash, recorded messages and creation time
SHALL be unchanged by a rename. A renamed channel SHALL therefore decrypt and post exactly what it
did before, and a running process SHALL use the new name without a restart, on the same terms as
any other channel configuration change.

The system SHALL refuse a rename whose new name is empty or only whitespace, and SHALL refuse a
rename to a name another channel already holds, because channel names are unique and channels are
addressed by name on the command line. A rename to the name the channel already holds SHALL be
accepted and change nothing.

A rename SHALL NOT render a stored pre-shared key anywhere, including in a form re-shown after a
refused rename.

#### Scenario: Renaming a channel
- **WHEN** a channel is renamed
- **THEN** it is listed under its new name with its kind, channel hash and recorded message count unchanged

#### Scenario: The key is untouched
- **WHEN** a pre-shared-key channel is renamed and a message is then received on it
- **THEN** the message is decrypted as it was before, because the rename changed no key material

#### Scenario: Renaming a hashtag channel
- **WHEN** a hashtag channel is renamed
- **THEN** its hashtag and its channel hash are unchanged, because the key derives from the hashtag and not from the name

#### Scenario: Renaming to a name already in use
- **WHEN** a rename is asked for with a name another channel already holds
- **THEN** the rename is refused and nothing is changed

#### Scenario: Renaming to an empty name
- **WHEN** a rename is asked for with an empty or whitespace-only name
- **THEN** the rename is refused and nothing is changed

#### Scenario: A refused rename discloses nothing
- **WHEN** a rename of a pre-shared-key channel is refused
- **THEN** no pre-shared key, in any encoding, is present in what is shown
