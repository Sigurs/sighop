# Spec Delta

## ADDED Requirements

### Requirement: History carries no real node identifiers
The system SHALL leave no real node identifier from the recorded corpus reachable from any ref, tag or
reflog entry of the repository. A real node identifier is a public key that appeared in a recorded
ADVERT, ANON_REQ or discovery response, any prefix of such a key, any node hash in a file derived from
the recording, or a position a recorded ADVERT carried. The set of real keys SHALL be derived by
decoding the recording itself, not from a hand-kept list. Where a rewritten file keeps a field's shape,
the real value SHALL be replaced by a synthetic stand-in of the same length rather than removed. The
mapping from real to synthetic values SHALL NOT be recoverable from the repository. The rewrite SHALL
change nothing else, and SHALL replace the original repository only after the operator has explicitly
confirmed it.

#### Scenario: No real public key or key prefix is reachable
- **WHEN** every revision of every file on every branch is searched for each real public key, in hex of either case and in base64, and for each key's 3-, 8- and 16-byte hex prefixes
- **THEN** none is found

#### Scenario: Recorded-era golden files hold only synthetic node hashes
- **WHEN** any revision of the golden file that was rendered from the recording is read
- **THEN** every `key=`, `claimed_key=`, `sender=`, `dest=`, `src=` and `path=` value is either `[redacted]` or a synthetic stand-in, and no `loc=` value other than `0,0` is a position from a recorded ADVERT

#### Scenario: Stand-ins keep the file's shape
- **WHEN** a rewritten golden revision is compared with its original
- **THEN** it has the same number of lines, the same fields in the same order and each hex value at the same length, and every field not named above is byte-identical

#### Scenario: Stand-ins are consistent
- **WHEN** the same real key or hash appears in several lines or several revisions
- **THEN** it is replaced by the same stand-in everywhere, and a hash that is a prefix of a real key is replaced by the same-length prefix of that key's stand-in

#### Scenario: Stand-ins cannot be reversed
- **WHEN** the rewritten repository is inspected in full
- **THEN** it contains neither the real-to-synthetic mapping nor the secret it was derived from, so even a 1-byte stand-in cannot be traced back to the real hash

#### Scenario: Nothing else changes
- **WHEN** the rewritten repository is compared with the original
- **THEN** every blob other than the recorded-era golden revisions is byte-identical, the tip tree is identical, and the branch list, commit count, authorship and messages match, except that commit hashes cited in messages point at the rewritten commits

#### Scenario: The rewrite waits for confirmation
- **WHEN** the rewritten repository has been produced and verified
- **THEN** the original is not replaced until the operator has confirmed, a backup of it exists outside the working tree, and the operator is told the backup still holds the real identifiers
