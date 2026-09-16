## Why

DESIGN.md promises channels in three places — §7's companion "joins channels", §8's chat client
"send DMs and channel messages", §5's channel-key and unverified-sender rules — but no section
designs them, so nothing decrypts `GRP_TXT` and the chat page says channels are absent. Channel
traffic is most of what a person on this mesh actually reads: of the corpus's 193 distinct
`GRP_TXT` frames, all 85 on channel hash `0x11` decrypt under the stock Public key, and sighop
shows every one of them as ciphertext. The protocol layer already parses group envelopes, derives
channel keys and splits `name: body`; what is missing is a key store, a receiver, a sender, a
history and a surface.

## What Changes

- **DESIGN.md gains a channels design** (§3 domain model, §5, §6 tables thirteen and fourteen, §7
  a new "Channels" behaviour section, §8 chat and admin, §9 events, §11 layout) and drops the
  statements that channels are absent. The bots paragraph is rewritten: `on_channel_message` stays
  absent by operator decision, no longer because the runtime cannot support it.
- **A platform-wide channel store.** Channels belong to the station, not to an identity: a
  `channel` row per channel, keyed by a Public preset, a hashtag, or a pre-shared key (16 or 32
  bytes). A pre-shared key is sealed at rest; hashtag and Public keys are stored as their public
  derivation. Removing a channel removes its history.
- **Public is present on upgrade.** The migration seeds the stock Public channel
  (`izOH6cXN6mrJ5e26oRXNcg==`). It transmits nothing: posting still requires the station's
  transmit gate, which is off on a fresh install.
- **Channel receptions are decrypted and recorded.** A bus subscriber trials every stored channel
  whose hash matches, verifies the MAC, parses plain group text, and records it once per channel
  with its claimed sender name marked unverified. `GRP_DATA` and non-plain group text are counted
  and left undecrypted, as the firmware does.
- **Chat in a channel as an identity.** An operator picks a loaded identity and posts; the post
  goes out as one flooded `GRP_TXT` reading `<identity name>: <text>`, class 2, no retry, no
  acknowledgement. It is a plain send like a direct message: refused before composition when the
  gate is closed, when `name: text` exceeds 160 bytes, or when the identity's name contains `": "`.
- **Our own post heard back is a repeat, not a message.** Posts are remembered by payload so a
  repeater's copy is counted as "heard repeated N×" on the post — the only delivery evidence a
  channel has — and never recorded as an inbound message from someone using our name.
- **Reception observers become a list**, so the channel repeat counter and the web feed both see
  every reception, duplicates included.
- **Web:** channels appear in chat beside direct conversations with a live-updating history and
  an identity-choosing composer; an admin page adds (Public, hashtag with its brute-force warning,
  pre-shared key) and removes channels. A stored pre-shared key is never shown in the browser.
- **CLI:** `sighop channel add|list|show|remove|key|history`. A pre-shared key is read from
  standard input or generated; `key` prints it for sharing.
- **Out of scope:** bot channel hooks and bot channel sends, channel webhook triggers, `GRP_DATA`,
  posting from the CLI, per-identity channel membership, and region-scoped (transport-code) floods.

## Capabilities

### New Capabilities
- `channel-store`: stored channel configuration — kinds, key derivation and sealing, the Public
  seed, duplicate refusal, removal, and how a running process learns of changes.
- `channel-messaging`: receiving (hash trial, MAC, parse, unverified sender), sending as an
  identity (composition, limits, flood, class, outcome), recognising our own posts' repeats, and
  the events and rendered output for both.
- `channel-history`: durable channel message records in both directions, written once and updated
  in place, bounded ordered reads, exposure statement, no pruning by default.

### Modified Capabilities
- `web-chat`: "Channels are absent" is removed; channel conversations, the channel composer and
  the unverified-sender presentation are added.
- `web-admin`: channels are configured through the interface; the list of capabilities not offered
  in the browser gains revealing a stored pre-shared key.
- `runtime-cli`: a channel command surface; startup reports the channels loaded; channel activity is
  rendered as run output.
- `net-bus`: every reception, duplicates included, is offered to any number of observers rather than
  one.
- `payload-codec`: group text bodies are built as well as parsed.
- `mesh-crypto`: channel decryption is proven against a foreign implementation (the corpus's Public
  channel frames).

## Impact

- `src/sighop/protocol/`: `PUBLIC_CHANNEL_KEY` constant, `build_group_text_body`; no new I/O.
- `src/sighop/net/channels.py` (new): `ChannelSet`, `ChannelMessenger` (receive, send, repeat
  registry, record sink list, typed events); `net/bus.py` observer list; `runtime.py` wiring,
  startup report, status counters, periodic channel reload.
- `src/sighop/db/`: models `Channel` and `ChannelMessage`, migration `0007`, `ChannelRepository`,
  `ChannelMessageRepository`, a channel-message writer lane; key sealing reuses value seal v2.
- `src/sighop/monitor/render.py`: decrypted channel lines with the claimed-name marking.
- `src/sighop/web/`: chat routes and templates for channels, `/admin/channels`, an in-memory
  channel log beside `ConversationLog`, `state.py` protocols.
- `src/sighop/cli.py`: `channel` noun.
- `src/sighop/bots/base.py`: docstring only — the reason `on_channel_message` is absent changes.
- Tests: protocol vectors from the corpus, messenger unit tests, repository tests, web route tests,
  a corpus replay asserting unchanged dedup/contact/path counts with channels wired.
- DESIGN.md. No new dependencies. Database schema moves `0006` → `0007`; a downgrade deletes all
  channels and channel history.
