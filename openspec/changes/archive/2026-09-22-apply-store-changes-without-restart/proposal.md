# Proposal

## Why

A run adopts its stored identities once, in `Runtime.__post_init__`, from the `stored_entities`
tuple `cli.py` built before the radio started. Rooms and bots are bound once, in `_restore`. So an
identity created or imported today does not advert, cannot be sent from and cannot carry traffic
until the process is restarted; a disabled or removed one keeps advertising; a newly created room
is not served and a newly created bot is not run. The operator's only way to apply any of it is to
take the station off the air.

The platform already knows how to do better in four places, and they disagree with each other:
channels reload live (immediately from this run's own panel, within 60 s for another process), an
identity rename applies live, a deleted room stops being served live, and a deleted bot stops being
run live. Everything else on those same three objects still needs a restart. This change makes the
whole of the identity, room and bot lifecycle behave the way the parts that were done already do.

## What Changes

- **Identities adopted and withdrawn mid-run.** A stored identity added, imported or enabled while
  a run is active is loaded into that run — advert schedule, inbound matching, path-body reading,
  channel posting and chat composition — without a restart. One disabled or removed is withdrawn
  from all of the same, and stops advertising at once.
- **Advert configuration applied mid-run.** A change to a stored identity's advert configuration
  takes effect on the live schedule, on the terms the rename already sets: the schedule is not
  reset, re-jittered or advanced as a side effect of the change itself.
- **Rooms served mid-run.** A room created while a run is active is served by it without a restart,
  the mirror of the deletion behaviour `room-server` already requires. A room whose identity is
  disabled or removed stops being served.
- **Bots run mid-run.** A bot created or enabled while a run is active starts running without a
  restart, the mirror of the deletion behaviour `bot-runtime` already requires. A bot disabled, or
  whose identity is disabled or removed, is stopped and finishes the dispatch in flight.
- **Two paths, one behaviour.** A change made through this run's own web interface applies
  immediately; a change made by another process — `sighop keys`, `sighop room`, `sighop bot`
  against the same database — is picked up by a periodic re-read, on the same contract
  `channel-store` sets. A re-read that cannot reach the database keeps what is loaded and reports
  the failure.
- **Live adoption is reported and refusable.** A change that adopts or withdraws anything is
  reported, naming what was added and removed; a re-read that changes nothing is silent. An
  identity whose node hash collides with one this run already holds is refused adoption with both
  named, rather than being adopted or ending the run — §3 rule 3 is a startup failure today, and a
  live adoption must not be able to kill a run that is on the air.

No breaking changes: every behaviour here is additive, and the restart path stays exactly as it is.

## Capabilities

### New Capabilities

None. The behaviour belongs to capabilities that already own these objects' lifecycles.

### Modified Capabilities

- `entity-store`: new requirement that a running process applies stored-identity changes — added,
  imported, enabled, disabled, removed, advert configuration changed — without a restart,
  immediately for its own web interface and within a bounded interval for another process, keeping
  the loaded set and reporting the failure when the store cannot be read. Today
  "Persisted entities carry their identity and advert configuration" describes restoration at
  startup only.
- `advert-policy`: new requirement that an identity adopted mid-run joins the advert schedule under
  the unchanged interval, jitter, floor and inter-entity gap rules and that one withdrawn mid-run
  leaves it immediately without disturbing the others; and that a live advert-configuration change
  applies without resetting the schedule, extending the contract the rename requirement already
  states. Also the refusal terms for a live adoption that would collide on node hash.
- `room-server`: new requirement that a room created while a run is active is served without a
  restart, and that a room whose identity stops being loaded stops being served — the counterpart
  of the existing "A running process stops serving it" scenario under room deletion.
- `bot-runtime`: new requirement that a bot created or enabled while a run is active is run without
  a restart, and that a bot whose enabled state or identity stops qualifying it is stopped with its
  in-flight dispatch finished — the counterpart of the existing deletion behaviour, and of
  "A disabled bot is loaded, reported and not run", which today is decided only at startup.
- `web-admin`: new requirements that an identity, room or bot write made through the interface
  reaches the running process without a restart and says so, and that a disabling which stops a
  served room or a running bot states that consequence as immediate — the guarantee `web-admin`
  already carries for adding a channel ("the running process decrypts on it without restart"),
  applied to the surfaces that still imply a restart.

Every delta is additive: each capability gains requirements and none of its existing requirements
is modified or removed. `web-chat`'s "A default identity that this run cannot use is not silently
substituted" already says the default applies *only while this run holds that identity*, and needs
no change — the set it quantifies over simply stops being fixed at startup.

## Impact

- `src/sighop/runtime.py`: `_adopt_entity` and the `__post_init__` loop become a reconcile step
  that can also withdraw; `_load_rooms` and `_load_bots` gain start-one counterparts to the
  existing `stop_serving_room` and `stop_bot`; a refresh loop alongside `_channel_refresh_loop`;
  `config.stored_entities` stops being the single source read once at construction.
- `src/sighop/net/adverts.py`: `AdvertScheduler` gains removal to match `add_identity` and
  `rename`, mutating `stubs` in place for the reason stated at `adverts.py:313` — `runtime.py`
  hands that list out by reference to the direct messenger, the channel messenger and the path-body
  reader, and replacing it would silently detach all three.
- `src/sighop/keystore.py`: `EntityRegistry` gains removal, and its §3 rule 3 check gains a
  non-fatal form for live adoption; `_register` currently can only raise.
- `src/sighop/web/state.py`: the `LiveState` protocol gains the methods the panel calls, beside
  `reload_channels`, `rename_entity`, `stop_serving_room` and `stop_bot`.
- `src/sighop/web/routes/keys.py` and `src/sighop/web/routes/admin.py`: create, import, enable,
  disable and remove call through the seam after the store has taken the write, as the rename and
  the deletions already do.
- **Hazard to resolve in design**: `runtime.py:508` and `runtime.py:977` *reassign*
  `path_bodies.entities` when a room takes or releases an entity, which breaks the aliasing to
  `adverts.stubs` that adoption otherwise relies on. Any run that has served or stopped a room
  would silently stop seeing newly adopted identities on the path-body path.
- Chat's `web_user.default_entity_id` already clears on identity deletion through its FK
  (`ON DELETE SET NULL`); a session carrying a default for an identity withdrawn mid-run needs to
  degrade rather than offer a composer for an identity the run no longer holds.
- **Drift found while planning**: a stored identity's `flood_interval_seconds` and
  `zero_hop_interval_seconds` are written by `advert_config_for`, displayed on the identity page and
  **never read** — `_adopt_entity` passes neither to `add_identity`, so every stored identity
  adverts at the 24 hour floor whatever its row says, and no surface writes a non-default interval
  either. "An advert configuration change applies live" is empty until the stored interval is read
  at all, so this change closes that gap. Adding a *control* that edits the interval stays out of
  scope; see design.md — Non-Goals.
- Tests: `tests/webfixtures.py` stubs the live seam and grows with it; the collision, refusal and
  reporting paths are new behaviour to cover.
