# Design

## Context

See `proposal.md` — Why. The constraints that actually shape the approach are all in the seam
between the panel and the running process:

- **`web/state.py` is a `Protocol` of read-only properties, deliberately.** Its docstring spells
  out why: a `Protocol` declaring a mutable attribute is invariant in its type, so a read-only
  property is what lets `Runtime` satisfy the seam without knowing it exists. There is exactly one
  mutating method on it today — `reload_channels()` — added because a channel change "has to reach
  decryption at once, and the runtime owns the loader". Every live effect this change needs takes
  the same shape.
- **`adverts.stubs` is the one live identity registry.** `Runtime._adopt_entity` registers each
  loaded identity as an `EntityStub`, and `runtime.py` passes `entities=self.adverts.stubs` to the
  direct messenger, the channel messenger, the room servers and the bot host. Those all hold the
  *same objects*: `net/dm.LocalEntity` is a structural `Protocol` declaring `entity_id: str` and
  `name: str` as mutable attributes, and its docstring says `EntityStub` satisfies it.
  `EntityStub` is `@dataclass(slots=True)` — mutable.
- **`keystore.LocalEntity` is a different type, and it is `frozen=True, slots=True`.** It has no
  `entity_id`, so it does not satisfy `dm.LocalEntity`; it is the registry's record of what was
  loaded and from where, read by the startup listing and the monitor renderer only.
- **An identity's name is mesh-visible in two places, not one.** It travels in the advert appdata,
  and `channels.py` builds every channel post body with `build_group_text_body(timestamp,
  entity.name, text)` after calling `check_sender_name(entity.name)`, which refuses a name
  containing the group-name separator because receivers split the message at the first one.
- **`EntityStub.entity_id` defaults to the identity's name.** `_adopt_entity` passes no
  `entity_id`, and `add_identity` falls back to `entity_id or name`. The advert confirmation URLs
  `/admin/advert/{entity_id}/{kind}` therefore carry the name, though `_advert_id` resolves a
  served room or running bot to its stub *by public key*.
- **Deletion needs no schema work.** `room.entity_id`, `room_member.room_id`, `message.room_id`,
  `bot.entity_id` and `bot_state.bot_id` are all `ON DELETE CASCADE` already. A room delete is one
  `DELETE`, a bot delete is one `DELETE`, a rename is one `UPDATE`.

## Goals / Non-Goals

**Goals:**

- One rename path and one delete path per kind, called by both the command line and the panel, so
  the parity `web-admin` requires is structural rather than remembered.
- A rename of a loaded identity reaches the air without a restart, and without disturbing that
  identity's advert schedule.
- Deletion of a room or a bot leaves its identity intact and reusable.

**Non-Goals:**

- Picking up a rename made by *another* process. A `sighop keys rename` run against a live
  station's database will not reach that station until it restarts. Channels have a refresh loop
  for this; entities have never had one, and adding one is a change of its own.
- Reworking how `entity_id` is derived. Decoupling it from the name is the better long-term shape
  and is explicitly deferred — see D2.
- Any change to sealing, public keys or node hashes.
- Refusing duplicate identity or room names at creation (D4).

## Decisions

### D1 — The live rename mutates the shared `EntityStub`; the frozen `LocalEntity` is replaced

A rename of a loaded identity sets `.name` on the one `EntityStub` in `adverts.stubs` whose
`identity.public_key` matches. Because the direct messenger, channel messenger, room servers and
bot host were all handed those same objects, one assignment reaches every consumer: the next
advert, the next channel post's sender name, and every log line that renders `entity.name`.

The keystore's `LocalEntity` is `frozen=True`, so the registry entry is *replaced* with a copy
carrying the new name. That is safe precisely because nothing holds it for behaviour — it feeds
the startup listing and the monitor renderer, both of which re-read the registry.

*Alternative considered:* dropping `frozen=True` from `LocalEntity` so a rename is one assignment
in both places. Rejected — the frozen record is doing its job, and replacing one registry entry is
cheaper than widening the mutability of a type that several modules read.

*Alternative considered:* re-registering the identity with `add_identity`. Rejected outright — it
resets the flood schedule and jitter, and `_register` would refuse it for a node-hash collision
with itself.

### D2 — `entity_id` moves with the name, and outstanding advert confirmations are allowed to fail

Since `_adopt_entity` derives `entity_id` from the name, leaving `entity_id` behind would give a
renamed identity a stub whose id silently disagrees with how every other identity's is formed.
The rename therefore sets both.

The consequence is that `/admin/advert/{entity_id}/{kind}` changes for a renamed identity, and a
confirmation nonce minted against the old id is refused. That is the safe direction: nonces are
target-bound by design, `_advert_id` resolves by public key at render time, so every page relinks
itself on the next render, and a refused stale confirmation is recorded like any other refusal.

*Alternative considered:* stop deriving `entity_id` from the name at all, giving stubs a stable
opaque id. Better, and out of scope — it changes advert URLs, log fields and `_advert_id`
consumers for every identity, not only renamed ones.

### D3 — Live effects reach the runtime through new seam methods, mirroring `reload_channels`

The panel may not import `runtime.py`. Three live effects are needed, and each becomes a method on
the `PanelState` seam alongside `reload_channels()`:

- `rename_entity(public_key, name) -> bool` — D1's mutation. Narrow and explicit rather than a
  `reload_entities()` re-read, because a re-read would mean re-opening sealed key material to
  change a label.
- `stop_serving_room(room_id) -> bool` — drops the `RoomServer` so a deleted room is no longer
  answered.
- `stop_bot(bot_id) -> bool` — awaits the existing `BotWorker.stop(deadline)`, which already
  finishes the dispatch in flight, then removes the worker from `BotHost` (which has `add` but no
  `remove` today).

Each returns a bool the way `reload_channels` does, so a page can report the durable change as
applied and the live effect as not, rather than pretending both happened.

### D4 — A name validator per kind, shared by create and rename

Implementation found that only half the kinds validate a name at all. `parse_channel_name` and
the webhook repository's `parse_name` refuse empty, over-length and control-character names and
strip surrounding whitespace; identities and rooms validate nothing, so an identity can be created
named `""` or named with the channel-post separator that `check_sender_name` refuses at post time.

So this adds `parse_entity_name` and `parse_room_name` in the same shape and the same place as the
two that exist, called from `EntityRepository.store` and `RoomRepository.create` as well as from
the new `rename` methods. Putting them in the repositories rather than in the surfaces is what
makes the refusal identical from the command line and the panel without either one remembering to
ask, which is the rule `web-admin` already states and the reason `store()` refuses a duplicate
public key where it does.

`parse_entity_name` additionally refuses the group-name separator, reusing the constant
`channels.py` already exports, so a name that could never post can no longer be stored. This is a
behaviour change to creation and import, not only to rename — see the proposal.

*Duplicate names are deliberately asymmetric.* Rename refuses a name another thing of the same
kind already holds; creation keeps whatever it does today, which for identities and rooms means
duplicates are tolerated. `_one_identity` already resolves an ambiguous reference by asking for a
longer public-key prefix, so duplicates are a state the system is built to survive; refusing them
at create would reject stores that already hold one. Refusing them at rename is still right,
because a rename is where an operator would newly introduce one, and for a *loaded* identity it is
a hard constraint rather than a preference — `entity_id` is the name (D2).

Renaming to the name already held is accepted as a no-op rather than refused as a duplicate: it is
what a resubmitted form does, and refusing it would be a confusing false negative.

### D5 — The rename applies first; the advert is a separate, already-guarded action

The panel's identity rename form carries the rename and an advert choice of none, zero-hop or
flood, with none selected by default. The handler applies the rename, then attempts the advert
through the existing requested-advert path, which keeps all four of its refusals (gate, readback,
identity loaded, inter-entity gap) and emits its own event.

Ordering matters and is fixed: rename first, so the advert carries the new name, and a refused
advert never rolls the rename back. The page reports the two outcomes separately. The form mints
one nonce per advert kind and spends the one chosen, because the choice is not known until submit.

*Alternative considered:* rename, then redirect to the existing standalone advert confirmation.
Simpler, and rejected because the operator would have to re-confirm an action they already chose,
which is the pattern that trains people to click through confirmations.

### D6 — Two confirmation tiers, matched to what is destroyed

- **Removing an identity** joins `REAUTHENTICATED_ACTIONS`: nonce *and* the operator's password,
  *and* the identity's name typed, because it destroys key material irrecoverably and the command
  line already asks for the typed name. This is the tier `REVEAL_KEY` and `EXPORT_KEY` are in.
- **Deleting a room or a bot** is nonce-only, no password, matching `REMOVE_CHANNEL`. They destroy
  stored content, not key material and not what the station may do. Their confirmation views carry
  the counts instead.
- **Rename** is neither: an ordinary configuration write, like setting retention. It is reversible
  by renaming back and destroys nothing, so it gets no nonce and no password, and is recorded in
  the request's own event with the old and new name.

### D7 — Deletion leans on the foreign keys rather than deleting by hand

`room_member` and `message` cascade from `room`; `bot_state` cascades from `bot`. The repositories
issue one `DELETE` and let the schema do the rest, which is also why the confirmation has to count
first — the cascade is silent. The counts come from the same `load_for_room` / `count` /
`message_count` calls the existing pages already use.

### D8 — No migration

Every column and constraint this needs exists. Nothing is added, dropped or altered, so there is
no Alembic revision and the expected schema revision is unchanged.

### D9 — The command line reuses the confirmation shape it already has

`sighop room delete` and `sighop bot delete` follow `_keys_delete` and `_channel_remove`: state the
cost, ask for the name at a terminal, accept an explicit flag where there is no terminal, and
refuse without deleting. The rename commands take the new name as an argument and ask for nothing,
because there is nothing to confirm.

## Risks / Trade-offs

- **A rename lands between a channel post's validation and its build** → `check_sender_name` and
  `build_group_text_body` both read `entity.name`. Keep the rename a single attribute assignment
  and assert in review that no `await` separates those two reads on the post path; if one does,
  read the name once into a local first.
- **A rename makes two loaded identities share a name, colliding their `entity_id`** → refuse the
  rename against the loaded stubs as well as the stored rows (D4).
- **An operator renames from a terminal while a station is live and the new name never reaches the
  air** → out of scope by decision, so say it: the rename command states that a running process
  keeps the old name until it restarts.
- **A stale advert link after a rename** → accepted (D2); the nonce is target-bound and the
  refusal is recorded.
- **Deleting a room that a member is mid-login against** → the login is answered or not depending
  on ordering, and after deletion the room answers nothing. No worse than the room being disabled,
  and the confirmation says the deletion is irreversible.
- **Withdrawing the "identity removal is not offered here" exclusion makes an irreversible action
  reachable in a browser** → mitigated by the strictest tier available (password plus typed name,
  D6) and by keeping the command line's refusal for a bound identity, which means the operator
  must delete the room or bot first and see that cost stated separately.
- **`BotHost` gains a `remove`** → the only structural addition to a lifecycle that currently only
  grows. Reuse `BotWorker.stop(deadline)` rather than cancelling, so the existing "a stopping bot
  worker finishes the dispatch it is running" requirement keeps holding.
