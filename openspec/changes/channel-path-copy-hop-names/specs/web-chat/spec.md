## MODIFIED Requirements

### Requirement: A received channel message's paths can be read and copied
The system SHALL, for a received channel message with at least one recorded path of one or more
hops, show its paths on hover of its received state, together with how many copies were heard, and
offer a control that copies them. The copied text SHALL be every recorded path in arrival order, one
per line. Each line SHALL give the hops in the order that copy carried them, joined by ` → `, each
hop written as its hash in lowercase hexadecimal, as wide as the hash size the message used, then a
space, then its label: the name of the one known node whose key starts with that hash, `<unknown>`
when no known node matches, `<ambiguous>` when more than one does, or the node's key prefix when the
one match has no name. A zero-hop path SHALL be written as `direct`. Hops SHALL be resolved against
the nodes known when the conversation is displayed, so a node learned later is named on the next
refresh. A name SHALL be copied with any control character replaced by a space, so a path stays on
one line. The hover SHALL give each path as its hop hashes alone, joined by ` → `, with `direct` for
a zero-hop path. The system SHALL NOT offer the control for a post from this station, for a message
whose every recorded path is zero-hop, or for a message with no recorded paths. Copying SHALL work
where the browser refuses clipboard access, falling back as copying a key does, and SHALL transmit
nothing.

#### Scenario: Copying one 3-hop path
- **WHEN** the operator uses the copy control on a message heard once, over hops `a3f1`, `28c0`, `9e4b`, where only `9e4b` matches a known node, named Glorfalas
- **THEN** `a3f1 <unknown> → 28c0 <unknown> → 9e4b Glorfalas` is copied

#### Scenario: Copying one path with known and unknown hops
- **WHEN** the operator uses the copy control on a message heard once, over hops `1337`, `0c90`, `afc6`, `bed0`, where only `afc6` (Kurala Hill repeater) and `bed0` (Glorfalas) match a known node
- **THEN** `1337 <unknown> → 0c90 <unknown> → afc6 Kurala Hill repeater → bed0 Glorfalas` is copied

#### Scenario: Copying several paths
- **WHEN** a message was heard over `a3`, `28`, `9e`, then directly, then over `a3`, `5d`, and no hop matches a known node
- **THEN** the copied text is the three lines `a3 <unknown> → 28 <unknown> → 9e <unknown>`, `direct`, `a3 <unknown> → 5d <unknown>`, in that order

#### Scenario: A hop matching several nodes or an unnamed node
- **WHEN** a copied path has one hop matching two known nodes and another matching one known node that never advertised a name
- **THEN** the first is labelled `<ambiguous>` and the second with that node's key prefix

#### Scenario: A node learned after the message
- **WHEN** a hop was unknown when the message arrived and its node is learned before the next refresh
- **THEN** after that refresh the copied path names it

#### Scenario: The paths on hover
- **WHEN** the operator hovers the received state of a message received over 2 hops and heard twice
- **THEN** the hover states it was received over 2 hops, that 2 copies were heard, and gives both paths as hashes joined by ` → `

#### Scenario: Nothing to copy
- **WHEN** a message heard only directly, a post from this station, or a message recorded without paths is displayed
- **THEN** no path copy control is shown for it and its state is shown as before

#### Scenario: A copy arriving while the channel is open
- **WHEN** a further copy of a displayed message is heard while the conversation is open
- **THEN** after the next refresh, without a reload, its copy control copies the new path too
