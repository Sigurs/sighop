## MODIFIED Requirements

### Requirement: Per-frame rendering
The system SHALL render each decoded frame as fixed-width, line-oriented text carrying the
timestamp, payload type, route type, hop count, path, SNR and RSSI, followed by the decoded
content of that payload type. An encrypted payload's line SHALL state that decoding did not
decrypt it and that keys are held elsewhere, rather than reporting that no key is held: the
decode stage holds no keys of any kind, and the layers that do — direct messages and channels —
report their own outcome on their own line.

#### Scenario: A verified advert is received
- **WHEN** an ADVERT with a valid signature is decoded
- **THEN** the rendered output shows a verification mark, the advert's name, its node type and its flags

#### Scenario: An encrypted payload is received
- **WHEN** an encrypted payload is decoded
- **THEN** the rendered output shows the destination or channel hash, the MAC and the ciphertext length, and states that it was not decrypted at decode because keys are tried by the layer that holds them

#### Scenario: A payload another layer goes on to decrypt
- **WHEN** a group text frame on a loaded channel is decoded and then decrypted by the channel layer
- **THEN** the frame's own line does not report a missing key, and the channel layer's line carries the decrypted message

#### Scenario: A frame fails to decode
- **WHEN** a frame fails structural decode
- **THEN** the rendered output shows the violated rule, the offset and the raw bytes, rather than omitting the frame
