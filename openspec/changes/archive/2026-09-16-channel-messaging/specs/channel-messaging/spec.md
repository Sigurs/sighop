## Purpose

Reading group text on the station's channels and posting into them as one of the station's
identities, with the protocol's limits stated rather than hidden: a channel message has no sender
authentication, no recipient and no acknowledgement.

## ADDED Requirements

### Requirement: Inbound group text is handled behind the bus, not in the decode stage
The system SHALL decrypt group text as a subscriber to receptions after deduplication, and SHALL NOT
decrypt group payloads in the decode stage, so that decoding remains a pure function of one frame and
a replayed capture reproduces every reception exactly.

#### Scenario: A replay with channels loaded
- **WHEN** the capture corpus is replayed with channels loaded
- **THEN** the considered, duplicate, contact and path counts are identical to a replay with no channels loaded

### Requirement: A reception is trialled against every channel whose hash matches
The system SHALL, for each `GRP_TXT` reception, attempt MAC verification under every loaded channel
whose channel hash equals the payload's, SHALL decrypt only under a channel whose MAC matched, and
SHALL stop at the first channel whose MAC matches and whose plaintext parses. A reception matching no
loaded channel hash SHALL be counted as on an unknown channel and left undecrypted.

#### Scenario: A Public channel frame from the corpus
- **WHEN** a `GRP_TXT` frame on channel hash `0x11` from the capture corpus is received with the Public channel loaded
- **THEN** it decrypts under Public and yields a claimed sender name and a body

#### Scenario: Two channels share a hash
- **WHEN** two loaded channels have the same channel hash and a reception was encrypted under the second
- **THEN** the first fails MAC verification, the second decrypts it, and the message is recorded in the second channel only

#### Scenario: An unknown channel
- **WHEN** a `GRP_TXT` reception's channel hash matches no loaded channel
- **THEN** it is counted as on an unknown channel, reported with its hash, and not shown as a chat message

#### Scenario: A hash matches but no MAC does
- **WHEN** a reception's channel hash matches a loaded channel and its MAC verifies under none
- **THEN** it is counted as undecryptable, reported with the channel hash, and not shown as a chat message

### Requirement: Only plain group text is shown, as the reference implementation does
The system SHALL present a decrypted group message only when its text type is plain, SHALL count and
report a decrypted group message of any other text type without presenting it, and SHALL NOT decrypt
`GRP_DATA` payloads.

#### Scenario: A non-plain text type
- **WHEN** a group message decrypts and its text type is not plain
- **THEN** it is counted and reported as an unsupported text type and does not appear in the channel

#### Scenario: Group data
- **WHEN** a `GRP_DATA` reception arrives on a loaded channel's hash
- **THEN** it appears in the packet feed as the undecrypted payload it is, and not in any channel

### Requirement: A channel sender name is a claim and is never presented as an identity
The system SHALL present the sender of a received channel message only as the name the plaintext
claims, marked as unverified wherever it is shown, and SHALL NOT link, colour, badge or otherwise
associate that name with a contact or a local identity of the same name. A message with no name
separator SHALL be presented with no sender.

#### Scenario: A claimed name equal to a verified contact's name
- **WHEN** a channel message claims the name of a contact whose advert was verified
- **THEN** it is shown with the unverified-claim marking and without the contact's verified marking

#### Scenario: A claimed name equal to a local identity's name
- **WHEN** a channel message that this station did not post claims the name of one of the station's identities
- **THEN** it is recorded and shown as a received message with an unverified claimed name, not as a message the station sent

### Requirement: A channel post is composed as the reference implementation composes one
The system SHALL compose a channel post from a loaded identity as group text whose timestamp is the
station's clock in epoch seconds, whose text type is plain with attempt zero, and whose text is the
identity's name, `": "`, and the composed text; SHALL encrypt it under the channel's key; and SHALL
carry it in one `GRP_TXT` packet under the channel's hash. Two posts by the station in the same
channel SHALL NOT carry the same timestamp.

#### Scenario: A post read back
- **WHEN** a post of `hello` by the identity `dev-companion` in a channel is decrypted under that channel's key
- **THEN** it parses as plain group text whose claimed sender is `dev-companion` and whose body is `hello`

#### Scenario: Two identical posts in one second
- **WHEN** the same identity posts the same text twice within one second in one channel
- **THEN** the two packets carry different timestamps, so a repeater does not discard the second as a duplicate of the first

### Requirement: A channel post that cannot be sent is refused before it is composed
The system SHALL refuse, with the reason stated and without truncating, a post whose identity name,
separator and text together exceed 160 bytes as UTF-8, a post by an identity whose name contains
`": "`, a post by an identity not loaded in this run, a post to a channel not loaded in this run, and
a post submitted while transmission is disabled. A refused post SHALL NOT be recorded.

#### Scenario: Text too long
- **WHEN** an identity named `dev-companion` composes 150 bytes of text
- **THEN** the post is refused stating the 160-byte limit, that the identity's name and separator count towards it, and how far over it is

#### Scenario: A name that would be split wrongly
- **WHEN** an identity whose name contains `": "` posts in a channel
- **THEN** the post is refused stating that receivers would split the name at that separator

#### Scenario: The transmit gate is closed
- **WHEN** a post is submitted while transmission is disabled
- **THEN** it is refused with that reason and nothing is queued

### Requirement: A channel post is one flooded class-2 transmission with no acknowledgement
The system SHALL submit a channel post flood-routed, at priority class 2, once, with no retry, and
SHALL NOT wait for or claim an acknowledgement. Its outcome SHALL be one of: awaiting transmission,
transmitted, or not transmitted with the scheduler's reason.

#### Scenario: A post transmitted
- **WHEN** a channel post's transmission completes
- **THEN** its outcome is transmitted, with no claim that anyone received it

#### Scenario: A post the budget drops
- **WHEN** the airtime budget cannot fit a channel post before its deadline
- **THEN** its outcome is not transmitted with the reason the scheduler gave, and it is not retried

### Requirement: The station's own posts heard back are counted as repeats, not received
The system SHALL remember each post it transmitted for at least one hour, bounded in number, by the
same payload identity deduplication uses, SHALL count every later reception of that payload —
duplicates included — as a repeat heard on that post, and SHALL NOT record or present such a reception
as a received channel message. The count SHALL be stated as evidence that a repeater forwarded the
post, not that any person received it.

#### Scenario: A repeater forwards a post
- **WHEN** the station transmits a channel post and then receives the same payload three times via repeaters
- **THEN** the post shows three repeats heard, and the channel shows no received message from the post's identity name

#### Scenario: No repeat heard
- **WHEN** no copy of a transmitted post is received
- **THEN** the post shows that no repeat was heard, and states that this does not mean it was not received

### Requirement: Channel activity is reported as events
The system SHALL report each channel reception outcome (decrypted, unknown channel, undecryptable,
unsupported text type), each post submitted or refused, each post outcome, each repeat heard, and each
adoption of a channel set that differs from the one in force, as a typed event carrying the channel
name or hash, the packet identifier, and for a post the identity that posted it. A claimed sender name
SHALL appear in an event only in a field whose name marks it as unverified.

#### Scenario: A decrypted reception
- **WHEN** a channel message is decrypted
- **THEN** one event names the channel, the packet identifier, the hop count, and the claimed sender name under an unverified field

#### Scenario: A set adopted from stored configuration
- **WHEN** a reload adopts a set that differs from the one in force
- **THEN** one event names the channels added, the channels removed, and how many are loaded; and the first set a run loads, which its startup report already names, produces no such event
