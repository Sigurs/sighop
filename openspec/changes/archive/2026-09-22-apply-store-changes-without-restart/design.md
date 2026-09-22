# Design

## Context

See `proposal.md` — Why. What shapes the approach is that four live-apply paths already exist and
established a shape worth repeating rather than replacing:

| Path | Trigger from this run's panel | Trigger from another process |
| --- | --- | --- |
| Channels | `LiveState.reload_channels()` | `_channel_refresh_loop`, `channel_refresh_seconds` |
| Identity rename | `LiveState.rename_entity()` | — (nothing; the rename is lost to this run) |
| Room delete | `LiveState.stop_serving_room()` | — |
| Bot delete | `LiveState.stop_bot()` | — |

So the panel half already has a seam — the `LiveState` protocol in `web/state.py`, which
`Runtime` satisfies structurally so that `runtime.py` never learns `web/` exists (design D2). The
cross-process half exists only for channels. This change extends the first and generalises the
second.

Four facts about the current code constrain the design:

1. **`adverts.stubs` is aliased on purpose.** `runtime.py` hands that very list to the direct
   messenger, the channel messenger and the path-body reader; `adverts.py:313` states that mutation
   must happen in place because of it. Adoption and withdrawal must mutate, never rebind.
2. **The alias is already broken in two places.** `runtime.py:508` and `runtime.py:977` *reassign*
   `path_bodies.entities` when a room takes or releases an entity. After a run has served or
   stopped one room, the path-body reader holds a different list, and an identity appended to
   `adverts.stubs` would never reach it.
3. **The collision rule is fatal by construction.** `EntityRegistry._register` and
   `AdvertScheduler.add_identity` can only raise; there is no non-fatal form. Startup failure is
   right at startup and wrong on the air.
4. **`stored_entities` is a construction-time tuple.** `cli.py` opens the sealed keys with
   `SIGHOP_SECRET_KEY` before `Runtime` exists, and `Runtime` never holds that secret — only
   `webhook_secret`, which is the same variable read for a different purpose.

An unplanned finding, recorded here because it changes what "advert configuration applies live"
can mean: **a stored identity's `flood_interval_seconds` and `zero_hop_interval_seconds` are
written, displayed and never read.** `advert_config_for` stores them, `identity.html:178` renders
the dict, and `_adopt_entity` passes neither to `add_identity`, so every stored identity adverts at
the 24 hour floor whatever its row says. Only `node_type` is read back. No surface writes a
non-default interval either.

## Goals / Non-Goals

**Goals:**

- One reconcile step that both halves call, so the panel path and the periodic path cannot drift
  into two behaviours the way channels and identities have.
- Withdrawal that is as complete as adoption — every registry an identity entered, it leaves.
- Failure that is contained: a bad row, a collision, or a degraded database leaves the run on the
  air with what it had.

**Non-Goals:**

- **No editing surface for advert intervals.** This change makes the stored interval *read* at
  adoption and *re-read* on a live change, closing finding (4) above. Adding a CLI or panel control
  that writes a non-floor interval is a separate change: it needs the override rules, the
  confirmation and the audit event that every other transmit-affecting control has.
- No change to the restart path. Startup still loads through `cli.py`; reconcile is what runs
  after.
- No live adoption of **keyfile** identities. A keyfile is passed on `run` and is not in the store;
  nothing polls the filesystem, and `local-identity` calls the keyfile an interchange format, not
  the store of record.
- No new capability spec. Every delta is additive to a capability that already owns the object.

## Decisions

### D1. One `reconcile_entities()`, not a create-hook per surface

The panel calls `reconcile_entities()` after its write; the refresh loop calls the same method on
the clock. Both compare the store's current openable set against `adverts.stubs` and apply the
difference.

*Why over the alternative:* the obvious cheaper design is `adopt_entity(record)` called by
`create_identity`, `import_identity` and `set_identity_enabled` with the row they just wrote. It is
less work and it is what the rename does. But it gives the panel path a code path the periodic path
does not have, and the periodic path has to compute a difference anyway. Two implementations of
"what should this run hold" is exactly the drift this change exists to remove — and the rename is
the reason the cross-process case was never built for identities. One reconcile, two callers.

The panel's call is then only a *promptness* optimisation, which makes the immediacy guarantee in
`entity-store` and `web-admin` cheap to hold and impossible to hold differently.

### D2. `entity_loader`, mirroring `channel_loader`

`Runtime` gains `entity_loader: Callable[[], Awaitable[OpenedEntities]] | None`, defaulted in
`__post_init__` from `persistence.entities.load_openable(secret, enabled_only=True)` exactly as
`channel_loader` defaults to `_load_stored_channels` at `runtime.py:371`.

*Why over holding the secret:* `Runtime` would otherwise need a second secret field beside
`webhook_secret` for the same environment variable, and `cli.py` is the module that composes both
sides (the reason `_web_sealing_secret` exists). A closure keeps the sealed-key concern in `cli.py`
and keeps `Runtime` testable with a stub loader — which is how `tests/webfixtures.py` already
stubs `reload_channels`.

`load_openable` already returns `opened` and `stranded`, so rows under the removed sealing format
and rows whose key does not open stay reported rather than silently dropped, on the terms
`entity-store` already sets.

### D3. Reconcile keyed by public key, diffed three ways

The store's openable set and `adverts.stubs` are matched on **public key**, not name and not row
id: the name is mutable and the row id is absent for keyfile identities. The diff yields:

- **to adopt** — openable, not held. Subject to D4.
- **to withdraw** — held, from the store, no longer openable. Keyfile-sourced stubs are never
  withdrawn, because they are not in the store's answer and their absence means nothing.
- **to re-configure** — held and openable, with a changed name, node type or advert interval.
  Applied in place through `AdvertScheduler.rename` and a new sibling that sets the intervals
  without touching `next_flood_at`, `adverts_sent` or any override, on the terms `advert-policy`'s
  rename requirement already sets for a rename.

Where the new interval makes the next advert due sooner, the next time is re-derived from the new
interval under the unchanged floor, jitter and gap rules, per the spec — not scheduled from the
moment of the change, which would let an interval edit become a way to advert.

### D4. A live collision is refused and remembered, not raised

`AdvertScheduler` and `EntityRegistry` gain a non-fatal admission check alongside the raising one:
reconcile asks first, and a colliding identity is reported once and recorded in a
`refused-by-public-key` set so the next re-read does not report it again. The set is cleared for a
public key when the identity it collided with is withdrawn, which is what makes the spec's
"A collision resolved" scenario work.

*Why not raise and catch:* an exception per re-read per colliding row, caught and suppressed, makes
"reported once" an accident of log filtering rather than a property. And the startup form must keep
raising — `runtime-cli` requires startup to *fail* naming both, and that requirement is unchanged.

### D5. Fix the `path_bodies.entities` rebinding before anything else

`PathBodyReader.__post_init__` copied its `entities` argument into a fresh list on construction —
a second break of the same alias, one step earlier than `stop_serving_room`/`_serve_room`, found
while implementing this task and not by the original planning pass. Fixed by copying only when the
caller did not already hand over a list, so `runtime.py`'s own list (the one it shares with
`adverts.stubs`) is kept rather than replaced.

That alone is not the whole fix. `stop_serving_room` and `_serve_room` used to *rebind*
`path_bodies.entities` to exclude a room-claimed entity from path-body matching; switching that to
`.remove()` / `.append()`, as first written here, mutates the same list object `adverts.stubs`
*is* — so claiming an entity for a room would remove it from `adverts.stubs` too, and a room
server's own identity would stop advertising the moment its room is served. Caught by the group 7
integration test, not by any unit test, because no existing test checked `adverts.stubs`
membership after a room was served. `DirectMessenger` already has this exact problem solved:
`claim_for_room`/`release_from_room` maintain a `_room_entity_ids` exclusion set, checked at read
time in `_candidates`, and never touch its `entities` list. `PathBodyReader` gained the identical
pair of methods and the identical exclusion set, checked in `_handle_path`; `stop_serving_room` and
`_serve_room` call them instead of mutating `path_bodies.entities` at all. This is finding (2), it
is a latent bug independent of this change — a run that has served and stopped a room today already
has a detached path-body entity list — and it is a prerequisite: adoption is otherwise silently
partial on exactly the runs that use rooms.

It gets its own task and its own test, ahead of the feature work, so the fix is not buried in it.

### D6. Withdrawal order is the reverse of adoption, and rooms and bots go first

Withdrawing an identity that a room or bot is bound to must stop the room or bot *before* the
identity leaves the registries, so the room's release path (`messenger.release_from_room`,
`path_bodies.entities`) still has something to release. Order:

1. stop any bot on the identity (awaiting the dispatch in flight, per `bot-runtime`);
2. stop serving any room on it (the existing `stop_serving_room`, which already unwires four
   attachments in a documented order);
3. remove the stub from `adverts.stubs` in place;
4. remove the entry from `EntityRegistry`.

An advert already submitted to the transmit scheduler is left to the scheduler. The spec says so
explicitly because the alternative — reaching into the queue — would make withdrawal a way to
cancel queued traffic, which nothing else in the platform can do.

### D7. Rooms and bots reconcile after identities, in the same pass

`reconcile_rooms()` and `reconcile_bots()` run after `reconcile_entities()` in one pass, reusing
`_serve_room` and `_run_bot` unchanged. Ordering matters and is already established by `_restore`:
rooms before bots, because `bot-runtime` refuses a bot on an entity a room holds and that is where
it is known. The spec's "the identity arrives after the room" and "the entity arrives after the
bot" scenarios both fall out of running the three in that order in one pass, rather than needing a
deferred-binding mechanism.

`RoomRetentionPruner` is constructed in `_load_rooms` only when `self.rooms` is non-empty; a first
room adopted mid-run must construct and start it, and a last room withdrawn must stop it.

### D8. `entity_refresh_seconds`, defaulting to the channel interval

A second interval rather than reusing `channel_refresh_seconds`, because they are separate
configuration for separate stores and coupling them would make one unchangeable without the other.
Default the same 60 s, so the guarantee the specs state is one number for an operator to remember.
The loop follows `_channel_refresh_loop` exactly, including sleeping on `self.clock` so a test
clock that does not advance never turns it into a database read per event-loop turn.

*Considered and rejected:* one loop reconciling everything including channels. It would couple a
channel read's failure to an identity read's, and `channel-store`'s "keep the last set" guarantee is
per-store.

### D9. Reporting reuses the `ChannelSetChanged` shape

An adoption or withdrawal produces an event naming what was added and removed; a reconcile that
changes nothing produces none. `net/channels.py`'s `ChannelSetChanged` exists for exactly this
reason — the CLI promises a change applies within 60 s, and without the event the run says nothing
when it does. Identities get the same treatment and the same silence when nothing changed.

Nothing in that event names key material; the withdrawal event names the identity and its node
hash, as the startup listing already does.

## Risks / Trade-offs

- **A reconcile racing a write in the same process.** The panel writes, then calls reconcile; the
  refresh loop may be mid-reconcile. → Guard reconcile with an `asyncio.Lock`; the second caller
  waits and then sees a set that already includes the write. Both callers are on the one event
  loop, so the lock is uncontended in the ordinary case.
- **Withdrawing an identity mid-send.** A direct message composed against a stub that is withdrawn
  before the acknowledgement arrives. → The expectation lives in `AckRegistry`, keyed by the
  message, not the stub; the send completes or times out on its own terms. Worth an explicit test
  rather than an assumption.
- **A room stopped and restarted by a disable/enable cycle logs every member out.** Members must
  log in again. → This is the same consequence a restart has, and `room-server`'s delta states that
  membership and history survive. Stated in the panel's confirmation per the `web-admin` delta, so
  it is not a surprise.
- **Reconcile cost grows with the stored set.** Every 60 s it opens every sealed key to compare.
  → `load_openable` is what startup already does once; at station scale (tens of identities) this
  is not a concern, but it is a real reason not to shorten the interval much. If it ever matters,
  compare on a cheap projection (id, name, enabled, node hash, config) and open only what changed.
- **The advert-interval finding widens the change.** Reading the stored interval at adoption means
  startup behaviour changes too: a row carrying a non-floor interval would start being honoured.
  → No surface writes one today, so no existing database has one, and `advert_config_for` defaults
  `flood_interval_seconds` to `None`. Treat `None` as the floor explicitly, and cover it with a test
  so the migration-free claim is checked rather than assumed.
- **Scope.** Five capabilities and three object lifecycles in one change. → The task breakdown
  sequences it so each lifecycle lands with its own tests and the run is releasable between them;
  D5 is first and standalone.

## Open Questions

- Whether a withdrawal should emit a final zero-hop advert so neighbours learn the identity is
  gone. It does not change the specs here (nothing requires it), and the mesh has no "identity
  withdrawn" semantics to carry it. Deferrable, and arguably a `advert-policy` question in its own
  right.
