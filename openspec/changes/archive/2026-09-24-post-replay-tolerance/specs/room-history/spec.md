# Spec Delta

## MODIFIED Requirements

### Requirement: A post from a permitted member is stored before it is acknowledged
The system SHALL treat a text message addressed to a room server from a member with posting
permission as a post, and SHALL acknowledge it **only after** it has been durably stored. Storage
SHALL NOT run on the path that receives and decodes packets, and SHALL be bounded by the sender's
own acknowledgement window; a post that cannot be stored within that window SHALL NOT be
acknowledged.

A post SHALL be accepted when its sender timestamp is no more than 300 seconds below the newest
sender timestamp recorded for that member, because a client stamps its posts and its logins and
keep-alives with different clocks. A post further below SHALL be refused as a replay, without an
acknowledgement, and the refusal SHALL state how far below the recorded timestamp it was. Whether
a post is a retry SHALL be decided by whether a post with the same author and sender timestamp is
already stored, not by comparing timestamps.

#### Scenario: A member posts
- **WHEN** a member with posting permission sends a text message to the room
- **THEN** the post is stored durably and only then acknowledged, and the acknowledgement is the one the sender computes for that exact transmitted plaintext

#### Scenario: Storage is unavailable
- **WHEN** a post arrives while durable storage is unavailable or too slow to complete inside the sender's acknowledgement window
- **THEN** nothing is acknowledged, nothing is stored, and the refusal is reported, so the sender reports a failure rather than believing a lost message was delivered

#### Scenario: A duplicate of an already-stored post
- **WHEN** the same post is retransmitted by its sender and recognised as a retry
- **THEN** it is acknowledged again and stored only once

#### Scenario: A post stamped by a clock behind the one that stamped the login
- **WHEN** a member's post carries a sender timestamp below the recorded one by 300 seconds or less, and no post with that timestamp is stored
- **THEN** it is stored and acknowledged, and the recorded timestamp is not lowered

#### Scenario: A retry of a stored post below the recorded timestamp
- **WHEN** a member retransmits a stored post whose sender timestamp is below the recorded one by 300 seconds or less
- **THEN** it is acknowledged again, reported as a retry, and stored only once

#### Scenario: A post far below the recorded timestamp
- **WHEN** a member's post carries a sender timestamp more than 300 seconds below the recorded one
- **THEN** it is refused as a replay, nothing is stored or transmitted, and the refusal states how many seconds below the recorded timestamp it was
