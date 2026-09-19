# Spec Delta

## ADDED Requirements

### Requirement: A bot can be deleted, and its durable state goes with it
The system SHALL provide an explicit action that deletes a bot. Deleting a bot SHALL delete
everything that bot has persisted with it, because durable state is namespaced per bot and means
nothing without the bot. The system SHALL state, before the deletion is applied, what that forgets
— for a greeter, every record of who has been greeted — and how many stored keys will be deleted.

Deletion SHALL be irreversible and SHALL say so before it happens. It SHALL require a deliberate
confirmation of that specific bot, and SHALL NOT be performed by a request that could be issued
incidentally.

Deleting a bot SHALL NOT delete the identity the bot was bound to. That identity SHALL remain
stored, keep its key material, and become unbound, so that it can carry a new bot or be removed in
its own right. A run running the deleted bot SHALL stop running it without being restarted, and
SHALL finish any dispatch already in flight rather than abandoning it mid-call.

#### Scenario: Deleting a bot
- **WHEN** a bot is deleted with confirmation
- **THEN** the bot and every key of its durable state are gone, and the bot no longer appears in the listing

#### Scenario: The cost is stated first
- **WHEN** deletion is offered for a bot that has persisted state
- **THEN** what that state records and how many keys will be deleted are stated before the deletion is applied

#### Scenario: Deletion without confirmation
- **WHEN** deletion is asked for without the confirmation for that bot
- **THEN** nothing is deleted and the refusal says the action was not confirmed

#### Scenario: The identity survives
- **WHEN** a bot is deleted
- **THEN** the identity it was bound to is still stored with its key material and its type unchanged, and is reported as running no bot

#### Scenario: The identity can carry a new bot
- **WHEN** a bot is deleted and a new bot is created on the same identity
- **THEN** the new bot is created, because the identity no longer carries one

#### Scenario: A running process stops running it
- **WHEN** a bot run by a running process is deleted while a dispatch is in flight
- **THEN** that dispatch finishes, no further dispatch is started for that bot, and the process keeps running its other bots

### Requirement: A bot is named by the identity it is bound to
The system SHALL NOT store a name for a bot separate from the identity it is bound to. Where a bot
is presented by name, that name SHALL be the bound identity's name, and where renaming a bot is
offered, it SHALL rename that identity and SHALL state that this is what it does, including that
the name is the one the identity adverts to the mesh.

#### Scenario: A bot presented by name
- **WHEN** a bot is listed or shown
- **THEN** the name it carries is the name of the identity it is bound to

#### Scenario: Renaming a bot
- **WHEN** renaming is offered for a bot
- **THEN** it states that it renames the bound identity and that the name is mesh-visible, and applying it renames that identity
