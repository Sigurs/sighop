# Spec Delta

## ADDED Requirements

### Requirement: A received channel message's paths can be read and copied
The system SHALL, for a received channel message with at least one recorded path of one or more
hops, show its paths on hover of its received state, together with how many copies were heard, and
offer a control that copies them. The copied text SHALL be every recorded path in arrival order, one
per line, each written as its hop hashes in lowercase hexadecimal, in the order that copy carried
them, joined by `->`, with each hash as wide as the hash size the message used; a zero-hop path
SHALL be written as `direct`. The system SHALL NOT offer the control for a post from this station,
for a message whose every recorded path is zero-hop, or for a message with no recorded paths. Copying
SHALL work where the browser refuses clipboard access, falling back as copying a key does, and SHALL
transmit nothing.

#### Scenario: Copying one 3-hop path
- **WHEN** the operator uses the copy control on a message heard once, over hops `a3f1`, `28c0`, `9e4b`
- **THEN** `a3f1->28c0->9e4b` is copied

#### Scenario: Copying several paths
- **WHEN** a message was heard over `a3->28->9e`, then directly, then over `a3->5d`
- **THEN** the copied text is the three lines `a3->28->9e`, `direct`, `a3->5d`, in that order

#### Scenario: The paths on hover
- **WHEN** the operator hovers the received state of a message received over 2 hops and heard twice
- **THEN** the hover states it was received over 2 hops, that 2 copies were heard, and gives both paths in the same form

#### Scenario: Nothing to copy
- **WHEN** a message heard only directly, a post from this station, or a message recorded without paths is displayed
- **THEN** no path copy control is shown for it and its state is shown as before

#### Scenario: A copy arriving while the channel is open
- **WHEN** a further copy of a displayed message is heard while the conversation is open
- **THEN** after the next refresh, without a reload, its copy control copies the new path too
