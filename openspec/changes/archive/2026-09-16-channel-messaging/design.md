## Context

See proposal.md for motivation. What already exists and shapes the approach:

- **Protocol is done.** `protocol/payloads.py` parses `GroupEnvelope` and `GroupTextBody` (with
  `unverified_sender_name`), `protocol/crypto.py` has `ChannelKey` (zero-extended `secret`, hash over
  the real length), `channel_key_from_hashtag`, `mac_then_decrypt`. Missing only a group text body
  builder and the Public key constant.
- **Corpus evidence, measured while planning.** 193 distinct `GRP_TXT` payloads: 85 on hash `0x11`
  all decrypt under Public (`izOH6cXN6mrJ5e26oRXNcg==`), claimed senders include `Sigurs`,
  `[redacted]`, `[redacted]`; 108 on `0x81` open under none of the obvious hashtags. Also measured: the Public
  hash over the zero-extended 32-byte buffer is `0x17`, and HMAC gives the same MAC for a 16-byte key
  and its zero extension (HMAC zero-pads keys itself), so only the hash length is a falsifiable
  negative.
- **Firmware behaviour** (`BaseChatMesh.cpp`, `Mesh.cpp`, `companion_radio/MyMesh.cpp`):
  receive trials up to 4 channels by hash and breaks on first MAC success; `onGroupDataRecv` drops
  `txt_type >> 2 != 0`; `sendGroupMessage` writes `"<name>: "` + text truncated so the whole is ≤
  `MAX_TEXT_LEN` (160), timestamp in plaintext "to make packet_hash unique", always `sendFlood`; no ack,
  no retry. The phone frame gives the text 176 − 11 = 165 bytes, so 160 is displayable by stock clients.
- **Direct messages are the template.** `net/dm.py` `DirectMessenger` is a bus subscriber with a list
  of record sinks (durable `WriteBehind` lane + web `ConversationLog`), records written at submission
  and updated in place on `UNIQUE (entity_public_key, ref)`, refused before composition in the web.
- **The dedup cache sees receptions only.** Our transmissions are never entered, so a repeater's copy
  of our flood arrives as a fresh reception and would decrypt as someone else's message.
- **One reception observer slot** (`IngressPipeline.observer`), owned by the web feed.
- **Memory is the authority** (§6): stores load at startup and answer from memory during an outage.
- Operator decisions for this change: channels are platform-wide; no bot changes; posting is a plain
  send; Public is added on upgrade.

## Goals / Non-Goals

**Goals:**
- Read every loaded channel on the air, record it, and show it without implying identity.
- Post as any loaded identity with firmware-identical bytes and firmware-compatible limits.
- Give the operator the one piece of delivery evidence a channel has — repeats heard — honestly labelled.
- Leave the reception pipeline's replay determinism and counts untouched.

**Non-Goals:**
- Bot hooks or bot channel sends; channel webhook triggers; `GRP_DATA`; transport-code scoped floods.
- CLI posting (a CLI process cannot reach a running run's radio; unlike a room post, a channel post
  has no stored-row delivery loop to hand it to).
- Per-identity channel membership or per-channel send flags.
- Re-authentication or confirm pages for posting.

## Decisions

### D1 — Channels are a station-level store, not identity state
`channel` rows carry no entity id. GRP_TXT has no recipient and its only "sender" is plaintext, so
per-identity membership would model something the wire cannot express; a handheld's channel slots are
a device property, and sighop is the device. Precedent: path knowledge is platform-wide (§4.2).
*Alternative:* per-identity joins — rejected by operator; would also force receptions to be stored
once per joined identity or attributed arbitrarily.

### D2 — Key storage by kind: seal secrets, not public derivations
Table `channel`: `id`, `name` (unique), `kind` (`public`|`hashtag`|`psk`, checked), `hashtag` (text,
required iff `hashtag`), `sealed_key` (bytea, value seal v2, required iff `psk`), `channel_hash`
(smallint, clear), `created_at`. Keys for `public` come from `protocol.PUBLIC_CHANNEL_KEY`; for
`hashtag` from `channel_key_from_hashtag`.
*Why:* a pre-shared key is a read-and-post credential, same class as a webhook URL, so it is sealed
like one. Sealing a key anyone can derive from the row's own name protects nothing — and storing
Public as a kind is what lets migration `0007` seed it **without `SIGHOP_SECRET_KEY`**, which
migrations do not have. `channel_hash` is clear so listing needs no secret.
*Duplicate key refusal* is in `ChannelRepository.add`: derive the candidate key, open/derive every
stored key (a handful of rows), compare in constant time. A same-hash, different-key channel is
accepted (§3: one byte collides).
*Alternative:* seal every kind — rejected, blocks seeding and gains nothing.

### D3 — `net/channels.py`: `ChannelSet` + `ChannelMessenger`
`ChannelSet` is an immutable snapshot `{hash: tuple[LoadedChannel, ...]}` swapped atomically;
`LoadedChannel(id, name, kind, key: ChannelKey)`. `ChannelMessenger` subscribes to the bus for
`GRP_TXT`, trials every channel under the hash (no firmware cap of 4 — a local store is small),
`mac_then_decrypt`, `parse_group_text_body`, requires `TextType.PLAIN`, and emits typed events
(`ChannelMessageReceived`, `ChannelUnknown`, `ChannelUndecryptable`, `ChannelUnsupportedText`,
`ChannelPostSubmitted`, `ChannelPostResolved`, `ChannelRepeatHeard`) that `monitor/render.py`
formats. `net/` imports nothing from `db/` or `web/`; storage is reached through record sinks and a
loader callable, exactly as `dm.py`. Same file rule as `dm.py`: never inside `net/rx.py`.
*Alternative:* extend `DirectMessenger` — rejected; it is 1255 lines of ack/route/retry logic none of
which applies, and a channel message has no peer key to key its records on.

### D4 — Configuration refresh: in-process push, cross-process poll at 60 s
`ChannelSet` loads at startup. Web admin changes call `runtime.reload_channels()` directly after the
repository commits (via a callable on `web/state.py`, keeping D2 of milestone 8: `web/` never imports
`runtime.py`). A background task reloads every 60 s so `sighop channel add` in another process lands
within a minute. A failed load keeps the previous snapshot and emits `channel_config_read_failed`.
*Why not read at event time like webhooks:* webhooks are off the reception path and tolerate a query
per event; decryption is a bus subscriber on every group reception and §6 says memory answers during
an outage. The 60 s bound matches session re-reads (§8).

### D5 — Composition and limits
`build_group_text_body(timestamp, sender_name, body)` in `protocol/payloads.py` (refuses `": "` in the
name). `ChannelMessenger.post(channel_id, identity, text)` checks, in order, before composing:
channel loaded, identity loaded, name has no `": "`, `len(f"{name}: {text}".encode()) <= 160`,
transmit gate open. Refusals are exceptions the web turns into the stated reason; nothing is recorded.
Timestamp is `max(now_epoch, last_post_timestamp + 1)` station-wide, so two identical posts never
share a packet hash (a repeater would drop the second). Submitted once, flood, class 2, the scheduler's
normal deadline; outcome `awaiting` → `transmitted` | `not_transmitted(reason)`.
*Why 160 rather than the 171-byte packet ceiling:* it is what stock senders produce and what stock
clients show in full; room posts already taught that an inherited constant must be checked against
both ends (§7), and here both ends agree on 160 as the sender limit. Refuse, never truncate — the
author is present (web-chat's rule).
*Why no 60-second-style cooldown:* operator chose plain send; the airtime budget and the gate are the
controls, as for direct messages.

### D6 — Own posts: a repeat registry fed by a reception observer
On `TxDone` success the messenger records `dedup_key(payload) → (post ref, channel id)` in a bounded
registry (256 entries, 1 h). Two consumers:
1. **Observer** (sees every copy, duplicates included): on a key hit, increment `repeats_heard`, emit
   `ChannelRepeatHeard` (hop count, SNR), offer an updated record to the sinks.
2. **Bus handler**: on a key hit, skip decryption and recording entirely.
The count lives only in the observer, so the first copy (non-duplicate, also fanned out) is not
counted twice. `IngressPipeline.observer` becomes `observers: list[RxObserver]`, each call isolated,
and `Runtime.watch_traffic` appends rather than replaces (net-bus delta).
*Why not seed the shared dedup cache with our transmissions:* it changes `rx-dedup` semantics for all
traffic, its 300 s TTL is shorter than the window a slow flood echo can arrive in, and it would make
our own adverts' echoes vanish from counters that currently show them. The registry is local to the
one feature that needs it.
*Why match by payload, never by claimed name:* a name is a claim anyone can make (§5); a message
claiming `dev-companion` that we did not post must appear as received.

### D7 — History: `channel_message`, the direct_message shape
Table `channel_message`: `id`, `channel_id` (FK `ON DELETE CASCADE`), `direction`, `ref` (outbound:
post id; inbound: packet id), `entity_public_key` (nullable, outbound), `unverified_sender_name`
(nullable text, inbound), `text` bytea, `wire_timestamp` bigint, `handled_at` timestamptz, `packet_id`,
`hop_count`, `snr_db`, `rssi_dbm` (inbound first copy), `outcome` (outbound), `repeats_heard` int
default 0, unique `(channel_id, ref)`, index `(channel_id, handled_at desc)`. Every write
`ON CONFLICT (channel_id, ref) DO UPDATE`. Written through a `WriteBehind` lane that **refuses** on
overflow (like contacts and DMs: content is not a sample) with counted, reported refusals. Restart
mid-flight: rows left `awaiting` are rewritten `unknown` at startup, as DMs.
The column is named `unverified_sender_name` for the same reason the dataclass field is: nobody can
read it without seeing the claim.
*Cascade on removal:* history is only meaningful under its key; a channel re-added later with the same
key is a new row. The confirmation states the count (channel-store delta).
Web keeps a `ChannelLog` (bounded per channel, like `ConversationLog`) as a second sink so channel chat
works during an outage.

### D8 — Web surface
Chat index gains a channels list (from the loaded `ChannelSet`, plus unread counts from `ChannelLog`).
Routes: `GET /chat/channel/{channel_id}`, `GET …/messages` (HTMX poll, same cadence as DMs),
`POST /chat/channel/{channel_id}` with `identity` form field and the session token — no nonce page.
Sender presentation: a dedicated `claimed-name` style that shares nothing with the verified identity
component (`_identity.html`), plus the standing sentence. Posts render under the identity using the
existing identity component, because *that* attribution is ours and true. Public composer carries a
"flooded to the whole mesh" line. Admin: `/admin/channels` list/add/remove, remove through
confirm-and-nonce stating the message count; PSK input is a password field never re-filled; the
"not offered here" list names `sighop channel key`. Removing the "channels absent" text and
`channels_absent` context.

### D9 — CLI
`sighop channel add --public | --hashtag NAME | --psk-stdin | --generate [--name NAME]`,
`list`, `show NAME`, `remove NAME [--delete-history]` (without the flag, prompts with the count; refuses
non-interactively), `key NAME`, `history NAME [--limit N]`. Generation: `secrets.token_bytes(16)`,
printed once in base64. All rules live in `ChannelRepository` so both surfaces refuse identically
(the `webui-write-parity` lesson).

### D10 — Replay and receive-only
Channel decryption runs in replay (pure over captured bytes) and records only when writes are enabled
(`--persist-replay`), the rule every other sink follows. Receive-only mode decrypts and records
normally; posting is refused before composition while the gate is closed.

### D11 — Bots and webhooks untouched, and the reason moves
`bots/base.py` and DESIGN.md §7 currently justify the absent `on_channel_message` by "nothing decrypts
GRP_TXT". After this change that is false. The docstring and §7 are rewritten: absent by operator
decision, deferred because a bot reacting to channel traffic is triggered by unauthenticated content
from anyone holding a (often guessable) key, and a bot posting floods the mesh — both deserve their own
change. No behaviour change.

### D12 — DESIGN.md is updated first
It is the contract (its own line 7). New §7 "Channels" section; §3 gains channels as station state;
§5 channel paragraph gains the measured hash/HMAC facts and the Public constant; §6 tables thirteen and
fourteen with the unencrypted-content statement and the downgrade loss; §8 chat client item 4 and the
"not exposed" list gain PSK reveal; §9 channel events; §11 `net/channels.py`, `web` additions; §12 a
note under milestone 8/9's finding that the chat page said channels were absent.

## Risks / Trade-offs

- **2-byte MAC false match on a shared hash** (~1/65536 per candidate) → plaintext must also parse as
  plain text with a sane length; a false decrypt that still parses is rendered as an unverified claim,
  which is exactly as trustworthy as a real one. Accepted, same as §3.
- **Public posting is one click from a mesh-wide flood** (operator chose plain send) → gate is off by
  default, composer states the audience, airtime ceiling bounds the worst case, every post is run
  output naming the account.
- **Repeat count under-reports** (repeaters out of our earshot, our deaf window during TX) → labelled
  as "heard", with "none heard ≠ not received".
- **Cross-process changes lag up to 60 s** → stated in CLI output after `add`/`remove`.
- **Cascade deletes history** → count stated and confirmed; downgrade statement in migration.
- **Stored channel text in the clear** → stated in migration docstring, same trade as `direct_message`.
- **Unknown `0x81` traffic stays ciphertext** → correct; it is someone else's private channel.
- **Observer list touches the hot path** → per-reception cost is one bytes-hash lookup for `GRP_TXT`
  only; corpus replay test asserts identical counts.

## Migration Plan

1. Migration `0007` (revises `0006`): create `channel`, `channel_message`; insert `('Public', 'public',
   hash 0x11)`. No secret required. Docstring states unencrypted content and that a downgrade deletes
   all channels and history.
2. Compose deployments pick it up with `run --migrate`; plain `run` refuses until `sighop db upgrade`.
3. Rollback: `sighop db downgrade 0006` with the previous image. Channels and history are lost; PSKs
   must be re-added from wherever they were shared.

## Open Questions

- What the `0x81` channel is (a regional hashtag?) — affects nothing but a nicer live exercise.
- Whether a busy Public channel makes `ChannelLog`'s per-channel bound (200, as DMs) too small for a
  useful outage view — observable only on a busy band.
