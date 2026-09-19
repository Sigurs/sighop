# Proposal

## Why

The panel can create every configurable thing the platform holds and remove almost none of it. An
identity can be made in the browser but only removed from a terminal, and that removal is refused
while a room or bot is bound to it — yet **nothing in any surface deletes a room or a bot**, so a
mistyped room is permanent and the identity under it is stranded with it. Nothing anywhere renames
anything, so a name chosen once at creation is the name forever, including the identity name that
travels in every advert and lands in every neighbour's contact list.

## What Changes

**Deletion**

- New: a room can be deleted, taking its members and its stored messages with it, in the command
  line (`sighop room delete`) and in the panel. The identity it was bound to survives, unbound, and
  can carry a new room.
- New: a bot can be deleted, taking its durable state with it, in the command line
  (`sighop bot delete`) and in the panel. The identity survives, unbound.
- New: a stored identity can be removed **from the panel**, with the refusals `sighop keys delete`
  already applies — an identity a room or bot is bound to is refused, naming what it serves.
- **BREAKING (spec-level, not API):** `web-admin` currently requires the panel to state that
  removing a stored identity is *deliberately not offered in the browser*. That exclusion is
  withdrawn. The four remaining exclusions (migrations, secret generation, account management,
  revealing a channel pre-shared key) stand unchanged.

**Renaming**

- New: a stored identity can be renamed, in the command line (`sighop keys rename`) and in the
  panel. An identity's name is mesh-visible, so this is the one rename with a consequence to state
  and the one that reaches the running process: the rename replaces the name the advert scheduler
  holds **without resetting that identity's flood schedule**, and the panel's rename form offers a
  zero-hop or flood advert in the same submission so the new name propagates at once. The advert is
  an existing guarded action with its own confirmation and its own refusals; the rename applies
  whether or not the advert does.
- New: a room can be renamed (`sighop room rename`, and in the panel). A room's name is a local
  label — it is not carried in a login response and not advertised — so it has no mesh consequence.
- New: a channel (`sighop channel rename`) and a webhook (`sighop webhook rename`) can be renamed,
  in both surfaces. Both names are already unique and purely labels; the channel hash and the
  sealed URL are untouched.
- A bot has **no name of its own** — it is named by the identity it is bound to. The panel's bot
  page therefore offers renaming that identity, and says that is what it is doing.

**Name validation, which turns out to exist for only half the kinds**

Channels and webhooks already validate a name wherever it is set (`parse_channel_name`,
`parse_name`): empty, over-length and control characters are refused, and a duplicate is refused
by name. **Identities and rooms validate nothing.** An identity can be created named `""` today,
or named with the separator a channel post puts between the sender and the text — which then
fails at post time with `SenderNameSeparatorError`, long after the name was chosen.

- New: `parse_entity_name` and `parse_room_name`, following the two that already exist, applied by
  **both** the create and the rename paths — so identity creation and import, and room creation,
  gain the refusals they lack today. For identities the validation also refuses the channel-post
  separator, closing that latent gap.
- A rename to a name already in use by the same kind of thing is refused. For identities this
  matters beyond tidiness: the advert scheduler keys an identity by its name. Creating a duplicate
  identity name stays tolerated, as it is today — `_one_identity` already resolves an ambiguous
  reference by asking for a longer key prefix — because refusing it at create would invalidate
  stores that already hold one.

## Capabilities

### New Capabilities

None. Every behaviour here extends a capability that already exists.

### Modified Capabilities

- `entity-store`: a stored identity can be renamed, with the refusals that keeps the store
  addressable; renaming touches no key material and no public key.
- `room-server`: a room can be deleted, stating what goes with it and leaving its identity unbound
  and reusable; a room can be renamed as a local label.
- `bot-runtime`: a bot can be deleted, taking its durable state, and leaving its identity unbound.
- `advert-policy`: renaming a loaded identity changes the name its adverts carry from the next
  advert onward without resetting its flood schedule or its jitter.
- `channel-store`: a channel can be renamed without changing its kind, key or channel hash.
- `webhooks`: a webhook can be renamed without changing its target, format or triggers.
- `runtime-cli`: the room and bot command surfaces gain `delete`; the keys, room, channel and
  webhook surfaces gain `rename`.
- `web-admin`: the panel deletes rooms, bots and identities, and renames identities, rooms,
  channels and webhooks; the requirement listing capabilities deliberately not offered in the
  browser drops identity removal from that list.

## Impact

**Code**

- `src/sighop/db/repositories.py` — new `rename` on the entity, room, channel and webhook
  repositories; new `delete` on the room and bot repositories.
- `src/sighop/cli.py` — six new subcommands, each following the confirmation pattern
  `_keys_delete` and `_channel_remove` already establish.
- `src/sighop/keystore.py` — a rename must replace the registry's `LocalEntity`, which is
  `frozen=True, slots=True`, rather than mutate it.
- `src/sighop/net/adverts.py` — `EntityStub` is mutable and its `entity_id` **defaults to the
  identity's name** (`runtime._adopt_entity` passes no id), so a rename updates the live stub in
  place; re-registering would reset the schedule and trip the node-hash collision check.
- `src/sighop/web/guarded.py` — new guarded actions for removing an identity, a room and a bot;
  removing an identity joins `REAUTHENTICATED_ACTIONS`.
- `src/sighop/web/routes/keys.py`, `src/sighop/web/routes/admin.py`, and new templates under
  `src/sighop/web/templates/admin/`.

**Not in scope**

- Deleting web accounts or contacts. Account management stays terminal-only as `web-admin`
  declares; a deleted contact returns on the next advert heard.
- Deleting individual messages or members. Member revocation already exists; retention already
  bounds history.
- Any change to the sealing format, the public key, or the node hash of an identity.
