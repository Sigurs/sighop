# Spec Delta

## ADDED Requirements

### Requirement: A room command deletes a room
The system SHALL provide a command that deletes one room, selected by name the same way the other
room commands select one. The command SHALL state before deleting how many members and how many
stored messages will be deleted with the room, SHALL confirm by asking for the room's name at a
terminal, and SHALL accept an explicit flag in place of that confirmation where no terminal is
available, in the same shape as the identity removal and channel removal commands. It SHALL delete
nothing whenever it refuses. It SHALL NOT delete the identity the room was bound to, and its
output SHALL name that identity and state that it is now unbound.

#### Scenario: Deleting a room
- **WHEN** the room deletion command is run and the confirmation matches the room's name
- **THEN** the room, its members and its messages are deleted, and the output names the identity that is now unbound

#### Scenario: Deletion states the cost first
- **WHEN** the room deletion command is run for a room holding members and messages
- **THEN** the counts of members and stored messages that will be deleted are stated before anything is deleted

#### Scenario: Deletion with no terminal and no flag
- **WHEN** the room deletion command is run without a terminal and without the flag that accepts the loss
- **THEN** it fails saying what the deletion would cost and how to accept it, and nothing is deleted

#### Scenario: Deletion confirmed with the wrong text
- **WHEN** the confirmation typed is not the room's name
- **THEN** nothing is deleted and the output says it was not confirmed

#### Scenario: Deleting a room that does not exist
- **WHEN** the room deletion command names no stored room
- **THEN** it fails saying so, and nothing is deleted

#### Scenario: No database configured
- **WHEN** the room deletion command is run with no database configured
- **THEN** it refuses and states that rooms require durable storage

### Requirement: A bot command deletes a bot
The system SHALL provide a command that deletes one bot, selected the same way the other bot
commands select one. The command SHALL state before deleting what the bot's durable state records
and how many stored keys will be deleted with it, SHALL confirm at a terminal, and SHALL accept an
explicit flag in place of that confirmation where no terminal is available. It SHALL delete
nothing whenever it refuses. It SHALL NOT delete the identity the bot was bound to, and its output
SHALL name that identity and state that it is now unbound.

#### Scenario: Deleting a bot
- **WHEN** the bot deletion command is run and is confirmed
- **THEN** the bot and its durable state are deleted, and the output names the identity that is now unbound

#### Scenario: Deletion states the cost first
- **WHEN** the bot deletion command is run for a bot that has persisted state
- **THEN** what that state records and how many keys will be deleted are stated before anything is deleted

#### Scenario: Deletion with no terminal and no flag
- **WHEN** the bot deletion command is run without a terminal and without the flag that accepts the loss
- **THEN** it fails saying what the deletion would cost and how to accept it, and nothing is deleted

#### Scenario: Deleting a bot that does not exist
- **WHEN** the bot deletion command names no stored bot
- **THEN** it fails saying so, and nothing is deleted

#### Scenario: No database configured
- **WHEN** the bot deletion command is run with no database configured
- **THEN** it refuses and states that bots require durable storage

### Requirement: The keys, room, channel and webhook surfaces each rename what they manage
The system SHALL provide a rename command on the identity, room, channel and webhook command
surfaces, each selecting its target the way that surface's other commands select one and each
taking the new name as an argument. A rename SHALL change the name and nothing else, SHALL NOT ask
for confirmation, and SHALL report the old and the new name.

Every refusal SHALL be the one the stored-configuration rules make, with its reason: an empty or
whitespace-only name, and a name already held by another thing of the same kind. A rename to the
name already held SHALL be accepted and change nothing.

Renaming an identity SHALL state that the name travels in that identity's adverts, that neighbours
will keep the old name until it adverts again, and SHALL name the command that adverts it now.
There SHALL be no rename command for a bot; the bot surface SHALL state that a bot is named by the
identity it is bound to and name the command that renames that identity.

#### Scenario: Renaming an identity
- **WHEN** the identity rename command is run with an unused name
- **THEN** the identity is renamed, the output reports the old and new name, states that neighbours keep the old name until the next advert, and names the command that adverts now

#### Scenario: Renaming a room, a channel or a webhook
- **WHEN** a rename command is run on the room, channel or webhook surface with an unused name
- **THEN** that thing is renamed, the output reports the old and new name, and nothing else about it is changed

#### Scenario: Renaming to a name in use
- **WHEN** a rename command is run with a name another thing of the same kind already holds
- **THEN** it fails with the stored-configuration rules' reason, and nothing is renamed

#### Scenario: Renaming to an empty name
- **WHEN** a rename command is run with an empty or whitespace-only name
- **THEN** it fails saying so, and nothing is renamed

#### Scenario: Looking for a bot rename
- **WHEN** an operator looks at the bot command surface for a way to rename a bot
- **THEN** there is no such command, and the help states that a bot is named by its identity and names the command that renames one

#### Scenario: No database configured
- **WHEN** a rename command is run with no database configured
- **THEN** it refuses and states that the thing it renames requires durable storage
